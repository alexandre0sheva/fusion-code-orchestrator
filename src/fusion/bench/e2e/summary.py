"""The latest Claude Code A/B result, for ``fusion stats`` and the dashboard.

Both read the benchmark store without writing to it. A study with real runs is preferred over a
simulated one, which is returned flagged so no one reads invented numbers as a measurement.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from fusion.bench.e2e.metrics import E2EArmRow, e2e_rows
from fusion.bench.store import BenchStore

LOOKBACK_RUNS = 50


class E2ESummary(BaseModel):
    """One ``fusion bench e2e`` run: its arms and what they cost per run."""

    run_id: str
    created_at: str
    mock: bool = Field(description="Simulated: the numbers are invented, not measured")
    rows: list[E2EArmRow]


def latest_e2e(bench_dir: Path) -> E2ESummary | None:
    """The newest end-to-end run in ``bench_dir`` (real before simulated), or None when there is
    none or no benchmark store yet. Never creates the store."""
    if not (bench_dir / "bench.db").is_file():
        return None
    store = BenchStore(bench_dir)
    try:
        fallback: E2ESummary | None = None
        for record in store.list_runs(limit=LOOKBACK_RUNS):
            rows = e2e_rows(store.items(record.run_id))
            if not rows:
                continue
            found = E2ESummary(
                run_id=record.run_id,
                created_at=record.created_at,
                mock=record.mock,
                rows=rows,
            )
            if not record.mock:
                return found
            fallback = fallback or found
        return fallback
    finally:
        store.close()


def best_value(rows: list[E2EArmRow]) -> E2EArmRow | None:
    """The arm that solved something at the lowest cost per solved task, or None."""
    priced = [r for r in rows if r.cost_per_solved_usd is not None]
    return min(priced, key=lambda r: r.cost_per_solved_usd or 0.0) if priced else None
