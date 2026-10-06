"""Which arm should be the default? The answer a dev study gives, and the reasoning behind it.

The rule is stated, not tuned: among the candidate arms, keep the cost-quality frontier (no other
candidate is at least as cheap per task and at least as good), then take the frontier arm with the
lowest cost per solved task whose quality is within the non-inferiority margin of the best
candidate's. The choice is never hidden when the data cannot support it: the recommendation says
when the chosen arm's interval overlaps the best one's, which at a dev study's size it usually does.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from fusion.bench.meta import ArmMeta
from fusion.bench.stats import DEFAULT_MARGIN, ArmStats, pareto_frontier

__all__ = ["Candidate", "Recommendation", "recommend"]


class Candidate(BaseModel):
    arm: str
    quality: float
    quality_low: float | None = None
    quality_high: float | None = None
    cost_per_task: float
    cost_per_solved: float | None
    on_frontier: bool
    within_margin: bool  # quality is within the margin of the best candidate's


class Recommendation(BaseModel):
    chosen: str | None
    reason: str
    margin: float
    best_quality_arm: str | None = None
    noisy: bool = False  # the chosen arm's interval overlaps the best arm's: the choice is a lean
    candidates: list[Candidate] = Field(default_factory=list)
    strategy: str | None = None  # what the chosen arm was built from, when the run recorded it
    overrides: dict[str, Any] = Field(default_factory=dict)

    def arm_line(self, name: str = "fusion-best") -> str | None:
        """The suite-file line that runs the chosen arm under ``name``."""
        if self.chosen is None or self.strategy is None:
            return None
        parts = [f"name: {name}", f"strategy: {self.strategy}"]
        if self.overrides:
            parts.append(f"overrides: {_flow(self.overrides)}")
        return "  - {" + ", ".join(parts) + "}"


def _flow(value: Any) -> str:
    """A YAML flow-style rendering, small and stable (keys in written order)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_flow(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_flow(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def recommend(
    arms: list[ArmStats],
    *,
    among: list[str] | None = None,
    margin: float = DEFAULT_MARGIN,
    arm_meta: list[ArmMeta] | None = None,
) -> Recommendation:
    """The arm to make the default, from a study's per-arm statistics."""
    pool = [
        a
        for a in arms
        if (among is None or a.arm in among)
        and a.mean_quality is not None
        and a.cost_per_task is not None
    ]
    if not pool:
        return Recommendation(
            chosen=None, margin=margin, reason="no candidate arm has both a quality and a cost"
        )
    best = max(pool, key=lambda a: a.mean_quality.estimate if a.mean_quality else 0.0)
    assert best.mean_quality is not None  # pool arms all have one
    floor = best.mean_quality.estimate - margin
    frontier = pareto_frontier(
        {
            a.arm: (a.cost_per_task.estimate, a.mean_quality.estimate)
            for a in pool
            if a.cost_per_task and a.mean_quality
        }
    )
    candidates = [
        Candidate(
            arm=a.arm,
            quality=a.mean_quality.estimate,
            quality_low=a.mean_quality.low,
            quality_high=a.mean_quality.high,
            cost_per_task=a.cost_per_task.estimate,
            cost_per_solved=a.cost_per_solved.estimate if a.cost_per_solved else None,
            on_frontier=a.arm in frontier,
            within_margin=a.mean_quality.estimate >= floor,
        )
        for a in pool
        if a.mean_quality and a.cost_per_task
    ]
    eligible = [c for c in candidates if c.on_frontier and c.within_margin]
    priced = [c for c in eligible if c.cost_per_solved is not None]
    if not priced:
        return Recommendation(
            chosen=None,
            margin=margin,
            best_quality_arm=best.arm,
            candidates=candidates,
            reason="no frontier arm within the margin of the best solved a task, so none has a "
            "cost per solved task",
        )
    chosen = min(priced, key=lambda c: (c.cost_per_solved or 0.0, -c.quality, c.arm))
    noisy = bool(
        best.mean_quality.low is not None
        and chosen.quality_high is not None
        and chosen.quality_high >= best.mean_quality.low
        and chosen.arm != best.arm
    )
    reason = (
        f"{chosen.arm}: the cheapest per solved task (${chosen.cost_per_solved:.4f}) of the "
        f"{len(eligible)} frontier arm(s) within {margin:g} quality of the best, {best.arm} "
        f"({best.mean_quality.estimate:.3f}); its own quality is {chosen.quality:.3f}."
    )
    if noisy:
        reason += (
            " Its interval overlaps the best arm's, so this study leans this way without "
            "showing that the two differ."
        )
    meta = next((m for m in arm_meta or [] if m.name == chosen.arm), None)
    return Recommendation(
        chosen=chosen.arm,
        margin=margin,
        best_quality_arm=best.arm,
        noisy=noisy,
        reason=reason,
        candidates=candidates,
        strategy=meta.strategy if meta else None,
        overrides=dict(meta.overrides) if meta else {},
    )
