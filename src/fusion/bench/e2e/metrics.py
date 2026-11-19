"""What one Claude Code run measured, and the same as the bench's ``BenchMetrics``.

Total cost is the host's cost plus Fusion's. The host's is Claude Code's own per-model token
counts (``modelUsage`` in its JSON) at the catalog's prices: Claude Code's ``total_cost_usd`` is
kept beside it (``host_cost_reported_usd``) but not trusted, because for a model it does not know it
uses a default price. Only a model the catalog lacks is counted at Claude Code's own figure.
Fusion's is read from the ledger the run's Fusion server wrote (its ``runs.db``, inside the
sandbox). Nothing is estimated: a host cost that cannot be priced either way stays unknown.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from statistics import fmean
from typing import Any

from pydantic import BaseModel, Field, ValidationError, computed_field

from fusion.bench.metrics import BenchMetrics
from fusion.bench.stats import percentile
from fusion.bench.store import BenchItem
from fusion.config.catalog import ModelEntry

__all__ = [
    "E2EArmRow",
    "E2EMetrics",
    "bench_metrics",
    "e2e_rows",
    "fusion_ledger",
    "price_host_usage",
]


class E2EMetrics(BaseModel):
    """One run of one arm on one task (stored as ``BenchItem.e2e``)."""

    executor: str  # opus | sonnet | haiku
    model_id: str
    fusion: bool
    host_cost_usd: float | None  # ``price_host_usage``; None: nothing could be priced
    host_cost_reported_usd: float | None = None  # Claude Code's own total_cost_usd
    host_usage: dict[str, Any] = Field(default_factory=dict)  # Claude Code's modelUsage
    fusion_cost_usd: float = 0.0
    wall_seconds: float
    turns: int = 0
    tool_calls: int = 0  # every tool call the host made, Fusion's included
    fusion_calls: int = 0  # Fusion runs in its ledger (one per tool call that reached a panel)
    fusion_tool_calls: int = 0  # Fusion tool calls in the transcript
    tests_passed: int = 0
    tests_expected: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    files_changed: list[str] = Field(default_factory=list)
    permission_denials: int = 0
    timed_out: bool = False
    claude_error: str | None = None
    fusion_connected: bool | None = None
    claude_seconds: float | None = None  # Claude Code's own duration, for comparison with wall

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_cost_usd(self) -> float:
        return (self.host_cost_usd or 0.0) + self.fusion_cost_usd

    @computed_field  # type: ignore[prop-decorator]
    @property
    def solved(self) -> bool:
        return self.tests_expected > 0 and self.tests_passed == self.tests_expected

    @property
    def quality(self) -> float:
        return self.tests_passed / self.tests_expected if self.tests_expected else 0.0


def _entry_for(model: str, models: Mapping[str, ModelEntry]) -> ModelEntry | None:
    """The catalog entry for a model id as Claude Code names it (a dated id matches too)."""
    for entry in models.values():
        if entry.provider == "anthropic" and (
            model == entry.model_id or model.startswith(f"{entry.model_id}-")
        ):
            return entry
    return None


def _tokens(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    return int(value) if isinstance(value, int | float) else 0


def price_host_usage(
    model_usage: Mapping[str, Any], models: Mapping[str, ModelEntry]
) -> tuple[float | None, list[str]]:
    """``(cost, models counted at Claude Code's own figure)`` for Claude Code's ``modelUsage``.

    Each model's tokens are priced at the catalog's base price (fresh input, cache reads, cache
    writes and output at their own rates). A model the catalog lacks counts at its ``costUSD``.
    The cost is None when there is no usage, or a model has neither a catalog price nor a figure.
    """
    if not model_usage:
        return None, []
    total = 0.0
    unpriced: list[str] = []
    known = True
    for model, raw in model_usage.items():
        usage = raw if isinstance(raw, Mapping) else {}
        entry = _entry_for(model, models)
        price = entry.price_at() if entry is not None else None
        if price is None:
            unpriced.append(model)
            figure = usage.get("costUSD")
            if isinstance(figure, int | float):
                total += float(figure)
            else:
                known = False
            continue
        total += (
            _tokens(usage, "inputTokens") * price.input_per_1m
            + _tokens(usage, "outputTokens") * price.output_per_1m
            + _tokens(usage, "cacheReadInputTokens")
            * (
                price.cached_input_per_1m
                if price.cached_input_per_1m is not None
                else price.input_per_1m
            )
            + _tokens(usage, "cacheCreationInputTokens")
            * (
                price.cache_write_per_1m
                if price.cache_write_per_1m is not None
                else price.input_per_1m
            )
        ) / 1_000_000
    return (total if known else None), unpriced


def fusion_ledger(db_path: Path) -> tuple[int, float]:
    """``(runs, cost)`` in a Fusion run database; ``(0, 0.0)`` when the server never wrote one."""
    if not db_path.is_file():
        return 0, 0.0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT COUNT(*), COALESCE(SUM(total_cost_usd), 0) FROM runs").fetchone()
    except sqlite3.OperationalError:  # the schema was never created: nothing ran
        return 0, 0.0
    finally:
        conn.close()
    return int(row[0]), float(row[1])


def bench_metrics(m: E2EMetrics, *, eval_seconds: float = 0.0) -> BenchMetrics:
    """The run as the bench's metrics, so the existing summaries and reports read it."""
    return BenchMetrics(
        seconds_to_complete=m.wall_seconds,
        cost_usd=m.total_cost_usd,
        cost_known=m.host_cost_usd is not None,
        eval_seconds=eval_seconds,
        input_tokens=m.input_tokens,
        output_tokens=m.output_tokens,
        reasoning_tokens=0,
        output_tokens_per_s=None,
        decode_tokens_per_s={},
        ttft_ms={},
        calls=m.turns + m.fusion_calls,
        retries=0,
        critical_path_ms=m.wall_seconds * 1000,
        quality=m.quality,
        solved=m.solved,
    )


class E2EArmRow(BaseModel):
    """One arm of an end-to-end study, summarised (means are per run)."""

    arm: str
    executor: str
    fusion: bool
    runs: int
    errors: int
    solved_rate: float | None  # of the runs that were scored
    host_cost_usd: float  # mean per run
    fusion_cost_usd: float
    total_cost_usd: float
    cost_per_solved_usd: float | None
    cost_known: bool  # False: some run printed no host cost, so the means are a lower bound
    seconds_p50: float | None
    turns: float
    tool_calls: float
    fusion_calls: float


def _parsed(item: BenchItem) -> E2EMetrics | None:
    if item.e2e is None:
        return None
    try:
        return E2EMetrics.model_validate(item.e2e)
    except ValidationError:
        return None


def e2e_rows(items: list[BenchItem]) -> list[E2EArmRow]:
    """Per-arm summary of the items of an end-to-end run, arms in order of first appearance;
    empty for a run with no end-to-end items."""
    groups: dict[str, list[tuple[BenchItem, E2EMetrics]]] = {}
    for item in items:
        metrics = _parsed(item)
        if metrics is not None:
            groups.setdefault(item.arm, []).append((item, metrics))
    rows: list[E2EArmRow] = []
    for arm, group in groups.items():
        runs = [m for _, m in group]
        scored = [(i, m) for i, m in group if i.metrics.quality is not None]
        solved = sum(1 for _, m in scored if m.solved)
        total = sum(m.total_cost_usd for m in runs)
        rows.append(
            E2EArmRow(
                arm=arm,
                executor=runs[0].executor,
                fusion=runs[0].fusion,
                runs=len(runs),
                errors=sum(1 for i, _ in group if i.status == "error"),
                solved_rate=solved / len(scored) if scored else None,
                host_cost_usd=fmean(m.host_cost_usd or 0.0 for m in runs),
                fusion_cost_usd=fmean(m.fusion_cost_usd for m in runs),
                total_cost_usd=total / len(runs),
                cost_per_solved_usd=total / solved if solved else None,
                cost_known=all(m.host_cost_usd is not None for m in runs),
                seconds_p50=percentile([m.wall_seconds for m in runs], 0.5),
                turns=fmean(m.turns for m in runs),
                tool_calls=fmean(m.tool_calls for m in runs),
                fusion_calls=fmean(m.fusion_calls for m in runs),
            )
        )
    return rows
