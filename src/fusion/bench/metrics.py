"""What is measured for one (task, arm, repeat), all of it from the run's own ledger."""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean

from pydantic import BaseModel

from fusion.orchestration.ledger import RunLedger

__all__ = ["BenchMetrics", "build_metrics"]


class BenchMetrics(BaseModel):
    """The measurements stored with every item. Nothing here is derived later from guesses."""

    seconds_to_complete: float  # wall time from request to final answer
    cost_usd: float  # every call the arm made; scorer and judge overhead is ``eval_cost_usd``
    cost_known: bool
    eval_cost_usd: float = 0.0
    # Wall seconds spent measuring and judging the answer (tests, timings, a browser, a judge). Eval
    # time, kept apart from ``seconds_to_complete``: it is never part of the arm's latency.
    eval_seconds: float = 0.0
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    output_tokens_per_s: float | None  # effective: output tokens divided by wall seconds
    decode_tokens_per_s: dict[str, float]  # per model alias, streamed calls only
    ttft_ms: dict[str, float]  # per model alias, streamed calls only
    calls: int
    retries: int
    critical_path_ms: float  # when the last call finished, on the run's own clock
    cache_hits: int = 0  # calls replayed from the response cache (billed at zero)
    # False when part of the run was replayed from the cache: its wall time is then not a
    # measurement of the arm's speed (per-call speed fields still are).
    latency_valid: bool = True
    quality: float | None = None
    solved: bool = False


def _mean_by_model(values: list[tuple[str, float | None]]) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for alias, value in values:
        if value is not None:
            grouped[alias].append(value)
    return {alias: fmean(v) for alias, v in sorted(grouped.items())}


def build_metrics(
    ledger: RunLedger,
    *,
    wall_ms: float,
    eval_cost_usd: float = 0.0,
    eval_seconds: float = 0.0,
    quality: float | None = None,
    solved: bool = False,
) -> BenchMetrics:
    """Metrics of a run from its ledger (shadow calls are never part of an arm's cost)."""
    base = ledger.task_metrics(wall_ms)
    counted = [r for r in ledger.records if r.stage not in ("shadow_baseline", "shadow_judge")]
    cache_hits = sum(1 for r in counted if r.cache_hit)
    return BenchMetrics(
        seconds_to_complete=base.seconds_to_complete,
        cost_usd=base.cost_usd,
        cost_known=base.cost_known,
        eval_cost_usd=eval_cost_usd,
        eval_seconds=eval_seconds,
        input_tokens=base.input_tokens,
        output_tokens=base.output_tokens,
        reasoning_tokens=base.reasoning_tokens,
        output_tokens_per_s=base.effective_output_tokens_per_s,
        decode_tokens_per_s=_mean_by_model(
            [(r.model_alias, r.decode_tokens_per_s) for r in counted]
        ),
        ttft_ms=_mean_by_model([(r.model_alias, r.ttft_ms) for r in counted]),
        calls=base.calls,
        retries=base.retries,
        critical_path_ms=base.critical_path_ms,
        cache_hits=cache_hits,
        latency_valid=cache_hits == 0,
        quality=quality,
        solved=solved,
    )
