"""Scores measured from an answer's claims. Used when no LLM judge scored the answer.

Every number is a share of the answer's claims that has a measurable property (cites a file,
carries evidence, proposes an action). Dimensions that cannot be measured without a judge or a
reference stay at a neutral 0.5; none of this depends on how an answer is worded.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fusion.orchestration.claims import PanelAnswer

NEUTRAL = 0.5
_ACTIONABLE = {"recommendation", "test"}


def _share(hits: int, total: int) -> float:
    return hits / total if total else 0.0


def structural_scores(
    answer: PanelAnswer | None,
    *,
    structured: bool,
    known_files: Collection[str] | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Judge-shaped scores derived from the claims of one answer."""
    if answer is None or not structured or not answer.claims:
        reason = "no claims to measure" if structured else "answer was not valid claims JSON"
        return _scores(dict.fromkeys(_DIMENSIONS, NEUTRAL), 0.0, f"{notes} Structural: {reason}")
    claims = answer.claims
    total = len(claims)
    located = sum(1 for c in claims if c.file)
    evidenced = sum(1 for c in claims if c.has_evidence)
    actionable = sum(1 for c in claims if c.kind in _ACTIONABLE)
    rated = sum(1 for c in claims if c.severity)
    known = {f.lower().lstrip("./") for f in known_files or ()}
    unknown = sum(
        1 for c in claims if c.file and known and c.file.lower().lstrip("./") not in known
    )
    values = {
        "specificity": _share(sum(1 for c in claims if c.file or c.evidence), total),
        "groundedness": _share(evidenced, total),
        "actionability": _share(actionable, total),
        "codebase_awareness": _share(located, total),
        "risk_awareness": _share(rated, total),
        "correctness_likelihood": NEUTRAL,
        "novelty": NEUTRAL,
    }
    return _scores(values, _share(unknown, total), f"{notes} Structural scores from {total} claims")


_DIMENSIONS = (
    "specificity",
    "groundedness",
    "actionability",
    "correctness_likelihood",
    "risk_awareness",
    "codebase_awareness",
    "novelty",
)


def _scores(values: dict[str, float], unsupported: float, notes: str) -> dict[str, Any]:
    measured = [values["specificity"], values["groundedness"], values["actionability"]]
    overall = sum(measured) / len(measured)
    return {
        **values,
        "unsupported_claims": unsupported,
        "overall_score": overall,
        "notes": notes.strip(),
    }
