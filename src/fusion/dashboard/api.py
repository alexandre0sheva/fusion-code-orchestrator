"""What the dashboard reads: small, read-only queries that return plain JSON-ready dicts.

Nothing here writes. The run database is opened ``mode=ro``, so even a bug cannot change it, and a
missing database is an empty dashboard, not a created file. The stored raw input of a run (what was
sent before redaction) is never selected unless ``FUSION_LOG_RAW_PROMPTS`` is true, the same switch
that governs what Fusion logs; what the dashboard shows by default is the redacted input.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fusion.config import paths as fusion_paths
from fusion.security.policy import SecurityPolicy
from fusion.storage.run_store import RunStore

RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
MAX_ROWS = 200
MAX_TEXT = 6000  # characters of one prompt field sent to the browser
MAX_ANSWER = 60_000
SHADOW_STAGES = ("shadow_baseline", "shadow_judge")


class NotFoundError(LookupError):
    """The thing asked for does not exist (or its id is not a plausible id)."""


# -- the database, read-only ------------------------------------------------------------------


class ReadOnlyStore(RunStore):
    """A ``RunStore`` whose connection cannot write; the dashboard's only way into the run DB."""

    def __init__(self, path: Path) -> None:
        super().__init__(str(path))
        self._path = path

    def _acquire(self) -> sqlite3.Connection:
        self._lock.acquire()
        try:
            if self._conn is None:
                conn = sqlite3.connect(
                    f"file:{self._path.as_posix()}?mode=ro", uri=True, check_same_thread=False
                )
                conn.row_factory = sqlite3.Row
                self._conn = conn
            return self._conn
        except BaseException:
            self._lock.release()
            raise

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        conn = self._acquire()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            self._release(conn)


@dataclass(frozen=True)
class Sources:
    """Where the data is, fixed when the app is created."""

    db_path: Path
    bench_dir: Path
    raw_prompts: bool

    @classmethod
    def resolve(cls, db_path: str | None = None, bench_dir: Path | None = None) -> Sources:
        return cls(
            db_path=fusion_paths.resolve_db_path(db_path),
            bench_dir=bench_dir or fusion_paths.bench_results_dir(),
            raw_prompts=SecurityPolicy.from_env().log_raw_prompts,
        )

    def store(self) -> ReadOnlyStore | None:
        """The run database, or None when nothing has been recorded yet."""
        return ReadOnlyStore(self.db_path) if self.db_path.is_file() else None

    @property
    def has_bench(self) -> bool:
        return (self.bench_dir / "bench.db").is_file()


def clean(value: Any) -> Any:
    """JSON-safe: NaN and infinities (which JSON cannot hold) become null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [clean(v) for v in value]
    return value


def cleaned(value: dict[str, Any]) -> dict[str, Any]:
    """``clean`` for a dict, keeping its type."""
    result: dict[str, Any] = clean(value)
    return result


def _guard(run_id: str) -> str:
    if not RUN_ID.fullmatch(run_id) or set(run_id) <= {"."}:
        msg = f"'{run_id}' is not a run id"
        raise NotFoundError(msg)
    return run_id


def wilson(successes: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """The Wilson score interval for a proportion (ties may count as half a success)."""
    if n <= 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


# -- meta ---------------------------------------------------------------------------------------


def meta(src: Sources) -> dict[str, Any]:
    from fusion import __version__

    return {
        "version": __version__,
        "db_path": str(src.db_path),
        "has_db": src.db_path.is_file(),
        "bench_dir": str(src.bench_dir),
        "has_bench": src.has_bench,
        "raw_prompts": src.raw_prompts,
    }


# -- overview ---------------------------------------------------------------------------------

_BASELINE = (
    "CAST(json_extract(output_json, '$.cost_comparison.baseline_estimated_cost_usd') AS REAL)"
)


def overview(src: Sources, days: int = 90) -> dict[str, Any]:
    store = src.store()
    if store is None:
        return {"empty": True}
    try:
        stats = store.get_stats()
        if stats.total_runs == 0:
            return {"empty": True}
        daily = store.query(
            f"""
            SELECT date(created_at) AS day, COUNT(*) AS runs,
                   COALESCE(SUM(total_cost_usd), 0) AS cost,
                   COALESCE(SUM(CASE WHEN {_BASELINE} IS NOT NULL THEN total_cost_usd END), 0)
                       AS paired_cost,
                   COALESCE(SUM({_BASELINE}), 0) AS baseline,
                   SUM(CASE WHEN {_BASELINE} IS NOT NULL THEN 1 ELSE 0 END) AS paired_runs
            FROM runs GROUP BY day ORDER BY day DESC LIMIT ?
            """,
            (days,),
        )
        by_strategy = store.query(
            """
            SELECT COALESCE(json_extract(routing_json, '$.strategy'), 'unknown') AS strategy,
                   COUNT(*) AS runs, COALESCE(SUM(total_cost_usd), 0) AS cost,
                   COALESCE(AVG(total_latency_ms), 0) AS avg_latency_ms
            FROM runs GROUP BY strategy ORDER BY runs DESC
            """
        )
        by_status = store.query("SELECT status, COUNT(*) AS n FROM runs GROUP BY status")
        recent = store.list_shadow_comparisons(limit=8)
        paired_runs = sum(r["paired_runs"] or 0 for r in daily)
        paired_fusion = sum(r["paired_cost"] for r in daily)
        paired_baseline = sum(r["baseline"] for r in daily)
        decided = stats.shadow_fusion_wins + stats.shadow_baseline_wins + stats.shadow_ties
        low, high = wilson(stats.shadow_fusion_wins + 0.5 * stats.shadow_ties, decided)
        return cleaned(
            {
                "empty": False,
                "totals": {
                    "runs": stats.total_runs,
                    "completed": stats.completed_runs,
                    "cost_usd": stats.total_fusion_cost_usd,
                    "avg_latency_ms": stats.avg_latency_ms,
                },
                "paired": {
                    "runs": paired_runs,
                    "fusion_usd": paired_fusion,
                    "baseline_usd": paired_baseline,
                    "savings_usd": paired_baseline - paired_fusion,
                    "savings_percent": (paired_baseline - paired_fusion) / paired_baseline * 100
                    if paired_baseline > 0
                    else None,
                },
                "daily": [dict(r) for r in reversed(daily)],
                "by_task": [
                    {
                        "task_type": t.task_type,
                        "runs": t.runs,
                        "cost_usd": t.total_cost_usd,
                        "avg_latency_ms": t.avg_latency_ms,
                    }
                    for t in stats.by_task_type
                ],
                "by_strategy": [dict(r) for r in by_strategy],
                "by_status": {r["status"]: r["n"] for r in by_status},
                "shadow": {
                    "total": decided,
                    "fusion_wins": stats.shadow_fusion_wins,
                    "baseline_wins": stats.shadow_baseline_wins,
                    "ties": stats.shadow_ties,
                    "win_rate": stats.shadow_win_rate_percent / 100
                    if stats.shadow_win_rate_percent is not None
                    else None,
                    "ci_low": low if decided else None,
                    "ci_high": high if decided else None,
                    "avg_fusion_score": stats.shadow_avg_fusion_score,
                    "avg_baseline_score": stats.shadow_avg_baseline_score,
                    "recent": [
                        {
                            "run_id": c.run_id,
                            "task_type": c.task_type,
                            "winner": c.winner,
                            "baseline_model": c.baseline_model,
                            "fusion_score": c.fusion_score,
                            "baseline_score": c.baseline_score,
                            "created_at": c.created_at,
                        }
                        for c in recent
                    ],
                },
            }
        )
    finally:
        store.close()


# -- runs ---------------------------------------------------------------------------------------


def runs(
    src: Sources,
    *,
    task: str | None = None,
    strategy: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    store = src.store()
    if store is None:
        return {"rows": [], "total": 0, "facets": {"task": [], "strategy": [], "status": []}}
    limit = max(1, min(limit, MAX_ROWS))
    offset = max(0, offset)
    where, params = [], []
    if task:
        where.append("task_type = ?")
        params.append(task)
    if strategy:
        where.append("COALESCE(json_extract(routing_json, '$.strategy'), 'unknown') = ?")
        params.append(strategy)
    if status:
        where.append("status = ?")
        params.append(status)
    if q:
        where.append("(run_id LIKE ? OR sanitized_input_json LIKE ?)")
        like = f"%{q.replace('%', '').replace('_', '')}%"
        params.extend([like, like])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    try:
        total = store.query(f"SELECT COUNT(*) AS n FROM runs {clause}", tuple(params))[0]["n"]
        rows = store.query(
            f"""
            SELECT run_id, task_type, status, created_at, total_cost_usd AS cost_usd,
                   total_latency_ms AS latency_ms,
                   COALESCE(json_extract(routing_json, '$.strategy'), 'unknown') AS strategy,
                   json_extract(output_json, '$.agreement.confidence') AS confidence,
                   {_BASELINE} AS baseline_usd,
                   json_extract(output_json, '$.usage.successful_model_calls') AS models_called
            FROM runs {clause} ORDER BY created_at DESC, run_id DESC LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        facets = {
            "task": [r[0] for r in store.query("SELECT DISTINCT task_type FROM runs ORDER BY 1")],
            "strategy": [
                r[0]
                for r in store.query(
                    "SELECT DISTINCT COALESCE(json_extract(routing_json, '$.strategy'), "
                    "'unknown') FROM runs ORDER BY 1"
                )
            ],
            "status": [r[0] for r in store.query("SELECT DISTINCT status FROM runs ORDER BY 1")],
        }
        return cleaned({"rows": [dict(r) for r in rows], "total": total, "facets": facets})
    finally:
        store.close()


def _cut(text: Any, limit: int = MAX_TEXT) -> dict[str, Any]:
    text = text if isinstance(text, str) else ""
    return {"text": text[:limit], "chars": len(text), "truncated": len(text) > limit}


def _prompt(record: Any, raw_ok: bool) -> dict[str, Any]:
    source = record.input_data if raw_ok else record.sanitized_input
    snippets = source.get("file_snippets") or []
    return {
        "raw": raw_ok,
        "primary": _cut(source.get("primary_content")),
        "context": _cut(source.get("context")),
        "snippets": [_cut(s) for s in snippets[:12] if isinstance(s, str)],
        "changed_files": [str(f) for f in (source.get("changed_files") or [])][:50],
        # How many secrets were replaced before any model saw this input (always the redacted
        # input's own count, even when the raw text is shown).
        "redaction_count": int(record.sanitized_input.get("redaction_count") or 0),
    }


def _timeline(output: dict[str, Any]) -> dict[str, Any] | None:
    calls = (output.get("ledger") or {}).get("calls") or []
    if not calls:
        return None
    rows = []
    for call in sorted(calls, key=lambda c: c.get("started_at_ms") or 0.0):
        start = float(call.get("started_at_ms") or 0.0)
        latency = float(call.get("latency_ms") or 0.0)
        out_tokens = call.get("output_tokens")
        speed = call.get("output_tokens_per_s")
        if speed is None and out_tokens and latency > 0:
            speed = out_tokens / (latency / 1000)
        rows.append(
            {
                "stage": call.get("stage"),
                "model": call.get("model_alias"),
                "provider": call.get("provider"),
                "start_ms": start,
                "end_ms": start + latency,
                "latency_ms": latency,
                "ok": bool(call.get("ok", True)),
                "status": call.get("status"),
                "error": (call.get("error") or "")[:300] or None,
                "input_tokens": call.get("input_tokens"),
                "output_tokens": out_tokens,
                "tokens_per_s": speed,
                "ttft_ms": call.get("ttft_ms"),
                "cost_usd": call.get("cost_usd") if call.get("cost_known", True) else None,
                "cache_hit": bool(call.get("cache_hit")),
                "retries": call.get("retries") or 0,
                "shadow": call.get("stage") in SHADOW_STAGES,
            }
        )
    return {"calls": rows, "total_ms": max(r["end_ms"] for r in rows)}


def _claims(output: dict[str, Any]) -> list[dict[str, Any]]:
    agreement = output.get("agreement") or {}
    group: dict[str, str] = {}
    for name in ("unique", "outliers", "contradicted", "consensus"):  # consensus wins ties
        for cid in agreement.get(name) or []:
            group[cid] = name
    claims = []
    for cluster in output.get("claims") or []:
        members = cluster.get("members") or []
        evidence = next(
            (m["claim"].get("evidence") for m in members if (m.get("claim") or {}).get("evidence")),
            None,
        )
        claims.append(
            {
                "id": cluster.get("id"),
                "kind": cluster.get("kind"),
                "text": cluster.get("text"),
                "severity": cluster.get("severity"),
                "file": cluster.get("file"),
                "line": cluster.get("line"),
                "models": cluster.get("models") or [],
                "group": group.get(cluster.get("id"), "unique"),
                "evidence": evidence,
            }
        )
    return claims


def run_detail(src: Sources, run_id: str) -> dict[str, Any]:
    _guard(run_id)
    store = src.store()
    record = store.get_run(run_id) if store else None
    if store is None or record is None:
        raise NotFoundError(f"No run '{run_id}'")
    try:
        created = store.query("SELECT created_at FROM runs WHERE run_id = ?", (run_id,))[0][0]
        shadow_rows = store.query(
            "SELECT winner, baseline_model, fusion_score, baseline_score, fusion_cost_usd, "
            "baseline_cost_usd, fusion_latency_ms, baseline_latency_ms FROM shadow_comparisons "
            "WHERE run_id = ?",
            (run_id,),
        )
    finally:
        store.close()
    output = record.output_data or {}
    answer = output.get("display_markdown") or output.get("final_answer") or ""
    return cleaned(
        {
            "run_id": record.run_id,
            "task_type": record.task_type,
            "status": record.status,
            "created_at": created,
            "cost_usd": record.total_cost_usd,
            "latency_ms": record.total_latency_ms,
            "strategy": (record.routing or {}).get("strategy"),
            "routing": {
                k: (record.routing or {}).get(k)
                for k in (
                    "strategy",
                    "complexity",
                    "risk",
                    "selected_panel",
                    "judge_model",
                    "synthesizer_model",
                    "reasons",
                )
            },
            "answer": _cut(answer, MAX_ANSWER),
            "confidence": (output.get("agreement") or {}).get("confidence"),
            "agreement": {
                k: (output.get("agreement") or {}).get(k)
                for k in (
                    "n_models",
                    "n_requested",
                    "n_clusters",
                    "score",
                    "evidence_rate",
                    "coverage",
                    "low_information",
                )
            },
            "claims": _claims(output),
            "prompt": _prompt(record, src.raw_prompts),
            "timeline": _timeline(output),
            "stages": [
                {"stage": name, **{k: v for k, v in totals.items() if k != "latency_ms"}}
                for name, totals in ((output.get("ledger") or {}).get("by_stage") or {}).items()
            ],
            "steps": [
                {
                    "step": s.step_name,
                    "model": s.model_name,
                    "input_tokens": s.input_tokens,
                    "output_tokens": s.output_tokens,
                    "cost_usd": s.cost_usd,
                    "latency_ms": s.latency_ms,
                }
                for s in record.steps
            ],
            "cost_comparison": output.get("cost_comparison") or None,
            "task_metrics": output.get("task_metrics") or None,
            "warnings": list(record.warnings),
            "partial": bool(output.get("partial")),
            "halt_reason": output.get("halt_reason"),
            "shadow": [dict(r) for r in shadow_rows],
        }
    )


# -- benchmarks ---------------------------------------------------------------------------------


def _bench_store(src: Sources) -> Any:
    from fusion.bench.store import BenchStore

    if not src.has_bench:
        return None
    return BenchStore(src.bench_dir)


def bench_runs(src: Sources) -> dict[str, Any]:
    store = _bench_store(src)
    if store is None:
        return {"runs": []}
    try:
        rows = []
        for r in store.list_runs(limit=100):
            rows.append(
                {
                    "run_id": r.run_id,
                    "status": r.status,
                    "stop_reason": r.stop_reason,
                    "dataset": r.dataset,
                    "mock": r.mock,
                    "total_jobs": r.total_jobs,
                    "done_jobs": r.done_jobs,
                    "spent_usd": r.spent_usd,
                    "eval_spent_usd": r.eval_spent_usd,
                    "created_at": r.created_at,
                    "arms": [a.name for a in r.config.arms],
                    "repeats": r.config.repeats,
                }
            )
        return cleaned({"runs": rows})
    finally:
        store.close()


def _bench_inputs(src: Sources, run_id: str) -> tuple[Any, Any, Any, Any]:
    """(store, record, items, meta) of a stored run; the caller closes the store."""
    from fusion.bench.cli import _run_inputs
    from fusion.config.layers import ConfigError

    _guard(run_id)
    store = _bench_store(src)
    if store is None or store.get_run(run_id) is None:
        if store is not None:
            store.close()
        raise NotFoundError(f"No benchmark run '{run_id}'")
    try:
        record, items, run_meta = _run_inputs(store, run_id)
    except ConfigError as exc:
        store.close()
        raise NotFoundError(str(exc)) from exc
    return store, record, items, run_meta


def bench_report(src: Sources, run_id: str) -> Any:
    """The report model of a run (``fusion.bench.report.Report``) and its run directory."""
    from fusion.bench.cli import _gate_for
    from fusion.bench.report import build_report
    from fusion.bench.stats import Rules as StatRules

    store, record, items, run_meta = _bench_inputs(src, run_id)
    try:
        report = build_report(
            record,
            items,
            meta=run_meta,
            gate=_gate_for(items, record.config.judge_models, record.mock),
            rules=StatRules(seed=record.config.seed),
            retried_after_halt=store.retried_after_halt(run_id),
        )
        return report, store.run_dir(run_id)
    finally:
        store.close()


def bench_compare(src: Sources, before: str, after: str) -> dict[str, Any]:
    from fusion.bench.report.compare import compare_runs
    from fusion.bench.stats import Rules as StatRules

    first = _bench_inputs(src, before)
    try:
        second = _bench_inputs(src, after)
    except NotFoundError:
        first[0].close()
        raise
    try:
        result = compare_runs(first[1:], second[1:], rules=StatRules(seed=first[1].config.seed))
        return cleaned(result.model_dump(mode="json"))
    finally:
        first[0].close()
        second[0].close()


# -- configuration ------------------------------------------------------------------------------


def config_view(src: Sources) -> dict[str, Any]:
    import os
    from datetime import date

    from fusion.cli.common import PROVIDER_KEYS
    from fusion.config.catalog import catalog_warnings, load_catalog
    from fusion.config.layers import resolve_config
    from fusion.config.loader import load_baseline
    from fusion.orchestration.strategy import load_strategy_book

    catalog = load_catalog()
    today = date.today()
    warnings = catalog_warnings(catalog)
    book = load_strategy_book()
    budgets_for: dict[str, list[str]] = {}
    for budget, name in book.budget_map.items():
        budgets_for.setdefault(name, []).append(budget)
    models = []
    for alias, entry in catalog.models.items():
        if entry.provider == "mock":
            continue
        price = entry.price_at(today)
        models.append(
            {
                "alias": alias,
                "provider": entry.provider,
                "model_id": entry.model_id,
                "enabled": entry.enabled,
                "roles": list(entry.roles),
                "input_per_1m": price.input_per_1m if price else None,
                "output_per_1m": price.output_per_1m if price else None,
                "verified_on": price.verified_on.isoformat()
                if price and price.verified_on
                else None,
                "warnings": [w for w in warnings if w.startswith(f"{alias}:")],
            }
        )
    baseline = load_baseline().baseline
    config = resolve_config()
    return cleaned(
        {
            "strategies": [
                {
                    "name": name,
                    "kind": book.get(name).kind,
                    "description": book.get(name).description,
                    "models": [m.model for m in book.get(name).members],
                    "rounds": book.get(name).rounds,
                    "aggregator": book.get(name).aggregator_model or book.get(name).aggregator,
                    "judge": book.get(name).judge,
                    "max_cost_usd": book.get(name).max_cost_usd,
                    "max_latency_s": book.get(name).max_latency_s,
                    "budgets": budgets_for.get(name, []),
                }
                for name in book.names()
            ],
            "default_strategy": book.budget_map.get("medium"),
            "models": models,
            "warnings": warnings,
            "baseline": {
                "name": baseline.name,
                "model_id": baseline.model_id,
                "enabled": baseline.enabled,
            },
            "layers": [
                {
                    "name": layer.name,
                    "path": str(layer.path) if layer.path else None,
                    "keys": sorted(layer.data),
                }
                for layer in config.layers
            ],
            "paths": {
                "user_config": str(fusion_paths.user_config_file()),
                "project_config": str(fusion_paths.project_config_file()),
                "database": str(src.db_path),
                "bench": str(src.bench_dir),
            },
            # Whether a key is present, never its value.
            "keys": {p: bool(os.environ.get(v, "").strip()) for p, v in PROVIDER_KEYS.items()},
            "raw_prompts": src.raw_prompts,
        }
    )
