"""A first reading of a run's items: one row per arm. Confidence intervals, per-task-type verdicts
and charts are the report's job; these are plain means and percentiles of what was measured."""

from __future__ import annotations

from statistics import fmean

from pydantic import BaseModel

from fusion.bench.costing import full_cost_usd
from fusion.bench.stats import percentile
from fusion.bench.store import BenchItem

__all__ = ["ArmSummary", "summarize"]


class ArmSummary(BaseModel):
    arm: str
    items: int
    completed: int  # ran to an answer (not halted, not an error)
    errors: int
    solved_rate: float | None
    mean_quality: float | None
    mean_cost_usd: float | None
    cost_per_solved_usd: float | None  # total cost of the arm's items / items solved
    eval_cost_usd: float
    mean_eval_cost_usd: float | None  # per item: what measuring and judging it cost
    mean_eval_seconds: float | None  # per item; never part of ``seconds_p50`` or ``seconds_p90``
    seconds_p50: float | None  # items replayed from the cache are left out: not a measurement
    seconds_p90: float | None
    output_tokens_per_s: float | None  # mean effective rate
    mean_calls: float | None
    cache_hits: int


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def summarize(items: list[BenchItem]) -> list[ArmSummary]:
    """Per-arm summary, arms in order of first appearance."""
    arms: dict[str, list[BenchItem]] = {}
    for item in items:
        arms.setdefault(item.arm, []).append(item)
    rows: list[ArmSummary] = []
    for arm, group in arms.items():
        scored = [i for i in group if i.metrics.quality is not None]
        solved = sum(1 for i in scored if i.metrics.solved)
        total_cost = sum(full_cost_usd(i) for i in group)
        timed = [i.metrics.seconds_to_complete for i in group if i.metrics.latency_valid]
        rates = [
            i.metrics.output_tokens_per_s
            for i in group
            if i.metrics.latency_valid and i.metrics.output_tokens_per_s is not None
        ]
        rows.append(
            ArmSummary(
                arm=arm,
                items=len(group),
                completed=sum(1 for i in group if i.status == "completed"),
                errors=sum(1 for i in group if i.status == "error"),
                solved_rate=solved / len(scored) if scored else None,
                mean_quality=_mean(
                    [i.metrics.quality for i in scored if i.metrics.quality is not None]
                ),
                mean_cost_usd=total_cost / len(group),
                cost_per_solved_usd=total_cost / solved if solved else None,
                eval_cost_usd=sum(i.metrics.eval_cost_usd for i in group),
                mean_eval_cost_usd=_mean([i.metrics.eval_cost_usd for i in group]),
                mean_eval_seconds=_mean([i.metrics.eval_seconds for i in group]),
                seconds_p50=percentile(timed, 0.5),
                seconds_p90=percentile(timed, 0.9),
                output_tokens_per_s=_mean(rates),
                mean_calls=_mean([float(i.metrics.calls) for i in group]),
                cache_hits=sum(i.metrics.cache_hits for i in group),
            )
        )
    return rows
