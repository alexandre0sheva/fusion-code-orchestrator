"""Neutral judges: a study's judges must come from vendors whose models serve no arm.

Judges favour answers in their own family's style, so a judge that shares a vendor with an arm is
not impartial about that arm. The cross-family rule (``scoring.pairwise.eligible_judges``) drops
such a judge from one comparison; ``neutral_judges: true`` in a study goes further and refuses
the study when any named judge shares a vendor with any arm, so every task of every arm is scored
by the same impartial judges. The vendor is ``ModelEntry.vendor``: the maker, not the API that
serves the model (Claude through OpenRouter is still Anthropic).

An arm's vendors are those of every model its strategy can call: members and aggregator, on every
rung of ``strategy: auto``'s ladder. A same-tier spare that stands in for a failed member is not
known in advance and is not checked; the bench records which model answered.
"""

from __future__ import annotations

from collections.abc import Mapping

from fusion.config.catalog import ModelEntry
from fusion.config.layers import ConfigError
from fusion.orchestration.strategy import Strategy
from fusion.routing.policy import RoutingPolicy

__all__ = ["arm_aliases", "check_neutral_judges", "judge_conflicts"]


def arm_aliases(strategy: Strategy, routing: RoutingPolicy) -> list[str]:
    """Every catalog alias a run of ``strategy`` may call: for ``kind: auto`` every climbable
    rung, whichever rung a request starts on (a hard one starts above rung 0)."""
    if strategy.kind != "auto":
        used = [strategy]
    else:
        policy = routing.escalation_for(strategy)
        used = [routing.strategies.get(policy.rungs[i].strategy) for i in policy.climbable()]
    return list(dict.fromkeys(a for s in used for a in s.models))


def judge_conflicts(
    judges: list[str],
    strategies: Mapping[str, Strategy],
    routing: RoutingPolicy,
    models: Mapping[str, ModelEntry],
) -> list[str]:
    """One line per judge that shares a vendor with an arm (empty: the judges are neutral)."""
    found: list[str] = []
    for judge in judges:
        entry = models.get(judge)
        if entry is None:
            continue  # an unknown judge is reported by the run itself
        clashes = [
            name
            for name, strategy in strategies.items()
            if any(
                models[a].vendor == entry.vendor
                for a in arm_aliases(strategy, routing)
                if a in models
            )
        ]
        if clashes:
            found.append(f"{judge} ({entry.vendor}) shares its vendor with {', '.join(clashes)}")
    return found


def check_neutral_judges(
    judges: list[str],
    strategies: Mapping[str, Strategy],
    routing: RoutingPolicy,
    models: Mapping[str, ModelEntry],
) -> None:
    """Raise ``ConfigError`` unless there is a judge and none shares a vendor with an arm."""
    if not judges:
        msg = "neutral_judges is set but the study names no judge_models"
        raise ConfigError(msg)
    conflicts = judge_conflicts(judges, strategies, routing, models)
    if conflicts:
        msg = "neutral_judges: " + "; ".join(conflicts)
        raise ConfigError(msg)
