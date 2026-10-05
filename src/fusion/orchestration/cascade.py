"""Cascade (FrugalGPT-style): ask the cheapest models first and stop when they agree.

The run asks the ``cascade.first`` cheapest members. If they agree closely enough on a task that
is not high risk, their answer is returned at once, with no synthesis call and none of the
dearer models asked. Otherwise the rest of the panel answers and the run continues as an
ordinary panel. This module holds the decisions; ``PanelStage`` runs the waves.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, Field

from fusion.config.loader import ModelEntry
from fusion.orchestration.claims import AgreementReport
from fusion.orchestration.fanout import FanoutResult
from fusion.orchestration.strategy import CascadeSpec, PanelMember
from fusion.routing.budget import price_per_million
from fusion.telemetry.cost import PricingRegistry

__all__ = ["CascadeOutcome", "cheapest_first", "decide", "merge_fanouts"]

_QUORUM_WARNING = "Panel quorum not met"


class CascadeOutcome(BaseModel):
    """What a cascade did and why; stored with the run."""

    exited_early: bool
    reason: str
    agreement: float
    threshold: float
    risk: str
    first_wave: list[str]
    escalated_to: list[str] = Field(default_factory=list)
    stopped_by_budget: bool = False  # the cost or latency cap ruled out escalating


def cheapest_first(
    members: Sequence[PanelMember],
    models: Mapping[str, ModelEntry],
    pricing: PricingRegistry,
) -> list[PanelMember]:
    """Members from cheapest to dearest by catalog list price; ties keep the configured order.

    A model with no price sorts last: it cannot be shown to be cheap.
    """

    def price(member: PanelMember) -> float:
        entry = models.get(member.model)
        found = price_per_million(entry, pricing) if entry else None
        return math.inf if found is None else found

    return sorted(members, key=price)  # sorted() is stable


def decide(report: AgreementReport, *, risk: str, spec: CascadeSpec) -> tuple[bool, str]:
    """Whether the first wave is enough: ``(exit_early, reason)``."""
    if report.n_models < 2:
        return False, f"only {report.n_models} of the first {spec.first} models answered"
    if report.contradicted:
        return False, f"{len(report.contradicted)} point(s) were disputed"
    if risk == "high" and spec.escalate_on_high_risk:
        return False, "the task is high risk"
    if report.score < spec.agreement_threshold:
        return (
            False,
            f"agreement {report.score:.2f} is below the {spec.agreement_threshold:.2f} threshold",
        )
    return (
        True,
        f"the first {report.n_models} models agree ({report.score:.2f}, threshold "
        f"{spec.agreement_threshold:.2f}); no synthesis call and no further models",
    )


def merge_fanouts(
    first: FanoutResult,
    second: FanoutResult,
    *,
    order: Sequence[str],
    min_successful: int,
) -> FanoutResult:
    """One fan-out result for two waves. The waves ran one after the other, so wall times add.

    The first wave was asked only for the quorum a cascade needs, so its quorum warning is dropped
    and the quorum is judged again over every model that was asked.
    """
    rank = {alias: i for i, alias in enumerate(order)}
    calls = sorted([*first.calls, *second.calls], key=lambda c: rank.get(c.model_name, len(rank)))
    needed = min(min_successful, max(len(calls), 1))
    successes = sum(1 for call in calls if call.success)
    warnings = [w for w in [*first.warnings, *second.warnings] if _QUORUM_WARNING not in w]
    if successes < needed:
        warnings.append(f"{_QUORUM_WARNING}: {successes}/{needed} successful responses.")
    return FanoutResult(
        calls=calls,
        panel_wall_latency_ms=first.panel_wall_latency_ms + second.panel_wall_latency_ms,
        total_model_call_latency_ms=sum(call.latency_ms for call in calls),
        max_model_latency_ms=max((call.latency_ms for call in calls), default=0),
        min_successful_responses=needed,
        quorum_met=successes >= needed,
        timed_out=first.timed_out or second.timed_out,
        early_return=first.early_return or second.early_return,
        hedged={**first.hedged, **second.hedged},
        warnings=warnings,
    )
