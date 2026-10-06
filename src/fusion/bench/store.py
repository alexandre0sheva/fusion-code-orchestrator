"""Where a study's results live: an append-only JSONL file per run, indexed in SQLite.

``bench-results/<run>/results.jsonl`` is the record of truth: one line per finished item, flushed
as it is written, so a killed run loses at most the item in flight and ``resume`` reads it back.
``bench-results/bench.db`` (the shared schema's ``bench_runs`` and ``bench_items`` tables) makes the
same data queryable. It is a separate file from the run history in ``runs.db`` so benchmark runs
never show up in ``fusion stats``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from fusion.bench.metrics import BenchMetrics
from fusion.bench.scoring import ScoreResult
from fusion.bench.spec import BenchConfig
from fusion.orchestration.ledger import CallRecord
from fusion.storage.run_store import RunStore, ShadowComparisonRecord
from fusion.storage.sqlite import get_connection

__all__ = [
    "BenchItem",
    "BenchRunRecord",
    "BenchStore",
    "InlineRunStore",
    "ItemStatus",
    "RunStatus",
    "new_bench_run_id",
]

ItemStatus = Literal["completed", "halted", "error"]
RunStatus = Literal["running", "completed", "stopped", "interrupted", "failed"]
DB_FILE = "bench.db"
RESULTS_FILE = "results.jsonl"
CONFIG_FILE = "config.json"
# ``error`` items are tried again on resume; the others are done.
FINISHED: frozenset[str] = frozenset({"completed", "halted"})


class BenchItem(BaseModel):
    """One (task, arm, repeat): the answer, how it was produced, what it cost and how it scored."""

    run_id: str
    job_key: str
    task_id: str
    category: str
    arm: str
    repeat: int
    seed: int
    status: ItemStatus
    error: str | None = None
    answer: str = ""
    claims: list[dict[str, Any]] = Field(default_factory=list)
    agreement: float | None = None
    warnings: list[str] = Field(default_factory=list)
    calls: list[CallRecord] = Field(default_factory=list)  # the full ledger of the arm's calls
    metrics: BenchMetrics
    score: ScoreResult | None = None


class BenchRunRecord(BaseModel):
    run_id: str
    status: RunStatus
    stop_reason: str | None = None
    dataset: str
    mock: bool
    config: BenchConfig
    total_jobs: int
    done_jobs: int
    spent_usd: float
    eval_spent_usd: float
    created_at: str
    updated_at: str


class BenchStore:
    """Reads and writes runs under one results directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # -- connection -------------------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = get_connection(self.root / DB_FILE)
        return self._conn

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                conn.close()

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    # -- runs -------------------------------------------------------------------------------

    def create_run(self, run_id: str, config: BenchConfig, total_jobs: int) -> None:
        """Register a run and write its configuration beside its (future) results."""
        folder = self.run_dir(run_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / CONFIG_FILE).write_text(config.model_dump_json(indent=1), encoding="utf-8")
        with self._lock:
            db = self._db()
            db.execute(
                "INSERT INTO bench_runs (run_id, status, dataset, mock, config_json, total_jobs)"
                " VALUES (?, 'running', ?, ?, ?, ?)",
                (
                    run_id,
                    str(config.dataset),
                    int(config.mock),
                    config.model_dump_json(),
                    total_jobs,
                ),
            )
            db.commit()

    def load_config(self, run_id: str) -> BenchConfig:
        path = self.run_dir(run_id) / CONFIG_FILE
        if not path.is_file():
            known = ", ".join(r.run_id for r in self.list_runs(limit=5)) or "none yet"
            msg = f"No benchmark run '{run_id}' under {self.root} (recent runs: {known})"
            raise FileNotFoundError(msg)
        return BenchConfig.model_validate_json(path.read_text(encoding="utf-8"))

    def update_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        stop_reason: str | None = None,
        total_jobs: int | None = None,
        done_jobs: int | None = None,
        spent_usd: float | None = None,
        eval_spent_usd: float | None = None,
        config: BenchConfig | None = None,
    ) -> None:
        columns: dict[str, Any] = {"status": status, "stop_reason": stop_reason}
        for name, value in (
            ("total_jobs", total_jobs),
            ("done_jobs", done_jobs),
            ("spent_usd", spent_usd),
            ("eval_spent_usd", eval_spent_usd),
        ):
            if value is not None:
                columns[name] = value
        if config is not None:
            columns["config_json"] = config.model_dump_json()
            (self.run_dir(run_id) / CONFIG_FILE).write_text(
                config.model_dump_json(indent=1), encoding="utf-8"
            )
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with self._lock:
            db = self._db()
            db.execute(
                f"UPDATE bench_runs SET {assignments}, updated_at = datetime('now')"  # noqa: S608
                " WHERE run_id = ?",
                (*columns.values(), run_id),
            )
            db.commit()

    def get_run(self, run_id: str) -> BenchRunRecord | None:
        with self._lock:
            row = (
                self._db()
                .execute("SELECT * FROM bench_runs WHERE run_id = ?", (run_id,))
                .fetchone()
            )
        return _run_record(row) if row else None

    def list_runs(self, limit: int = 20) -> list[BenchRunRecord]:
        with self._lock:
            rows = (
                self._db()
                .execute(
                    "SELECT * FROM bench_runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
                    (limit,),
                )
                .fetchall()
            )
        return [_run_record(r) for r in rows]

    # -- items ------------------------------------------------------------------------------

    def add_item(self, item: BenchItem) -> None:
        """Append the item to the run's results file (flushed to disk), then index it."""
        line = item.model_dump_json()
        path = self.run_dir(item.run_id) / RESULTS_FILE
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
            self._index(item, line)

    def _index(self, item: BenchItem, line: str) -> None:
        db = self._db()
        db.execute(
            "INSERT OR REPLACE INTO bench_items (run_id, job_key, task_id, category, arm, repeat,"
            " status, quality, solved, cost_usd, eval_cost_usd, seconds, item_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.run_id,
                item.job_key,
                item.task_id,
                item.category,
                item.arm,
                item.repeat,
                item.status,
                item.metrics.quality,
                int(item.metrics.solved),
                item.metrics.cost_usd,
                item.metrics.eval_cost_usd,
                item.metrics.seconds_to_complete,
                line,
            ),
        )
        db.commit()

    def items(self, run_id: str) -> list[BenchItem]:
        """Every item of a run, the latest attempt of each job, in the order first written.

        A line cut off by a crash is skipped.
        """
        path = self.run_dir(run_id) / RESULTS_FILE
        if not path.is_file():
            return []
        latest: dict[str, BenchItem] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                item = BenchItem.model_validate_json(raw)
            except ValueError:
                continue
            latest[item.job_key] = item
        return list(latest.values())

    def spent(self, run_id: str) -> tuple[float, float]:
        """``(arm cost, scorer cost)`` over every attempt written, including ones a retry replaced:
        the money was spent whether or not the attempt succeeded."""
        path = self.run_dir(run_id) / RESULTS_FILE
        cost = eval_cost = 0.0
        if path.is_file():
            for raw in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = BenchItem.model_validate_json(raw)
                except ValueError:
                    continue
                cost += item.metrics.cost_usd
                eval_cost += item.metrics.eval_cost_usd
        return cost, eval_cost

    def retried_after_halt(self, run_id: str) -> dict[str, int]:
        """Arm -> jobs whose first attempt halted and that were then run again (see ``resume
        --retry-halted``). Every attempt stays in ``results.jsonl``; only the last is reported."""
        path = self.run_dir(run_id) / RESULTS_FILE
        first: dict[str, BenchItem] = {}
        again: set[str] = set()
        if path.is_file():
            for raw in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = BenchItem.model_validate_json(raw)
                except ValueError:
                    continue
                if item.job_key in first:
                    again.add(item.job_key)
                else:
                    first[item.job_key] = item
        out: dict[str, int] = {}
        for key in again:
            if first[key].status == "halted":
                out[first[key].arm] = out.get(first[key].arm, 0) + 1
        return dict(sorted(out.items()))

    def completed_keys(self, run_id: str, *, retry_halted: bool = False) -> set[str]:
        """Job keys that need no more work. ``retry_halted`` leaves out the halted ones (a panel
        that did not reach quorum, a call that timed out) so a resume tries them again."""
        done = {"completed"} if retry_halted else FINISHED
        return {i.job_key for i in self.items(run_id) if i.status in done}

    def sync_index(self, run_id: str) -> None:
        """Re-index the results file (after a crash between writing a line and indexing it)."""
        with self._lock:
            for item in self.items(run_id):
                self._index(item, item.model_dump_json())


def _run_record(row: sqlite3.Row) -> BenchRunRecord:
    return BenchRunRecord(
        run_id=row["run_id"],
        status=row["status"],
        stop_reason=row["stop_reason"],
        dataset=row["dataset"],
        mock=bool(row["mock"]),
        config=BenchConfig.model_validate(json.loads(row["config_json"])),
        total_jobs=row["total_jobs"],
        done_jobs=row["done_jobs"],
        spent_usd=row["spent_usd"],
        eval_spent_usd=row["eval_spent_usd"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def new_bench_run_id(now: datetime | None = None) -> str:
    """``bench-20261005-143015-ab12``: sorts by time, unique enough for one machine."""
    moment = now or datetime.now(UTC)
    return f"bench-{moment:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"


class InlineRunStore(RunStore):
    """A run store whose async methods run on the calling thread.

    Simulated studies run on the virtual-time loop, which cannot wait for a worker thread, and a
    local SQLite write is fast enough not to need one.
    """

    async def acreate_run(self, **kwargs: Any) -> str:
        return self.create_run(**kwargs)

    async def acomplete_run(self, run_id: str, **kwargs: Any) -> None:
        self.complete_run(run_id, **kwargs)

    async def arecord_shadow_comparison(self, record: ShadowComparisonRecord) -> None:
        self.record_shadow_comparison(record)
