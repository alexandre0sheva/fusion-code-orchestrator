"""Outcome evaluation hooks for future integration."""

from __future__ import annotations

from fusion.evals.schemas import OutcomeEvalResult


def evaluate_outcome(*, run_id: str) -> OutcomeEvalResult:
    """Placeholder for future outcome-based evaluation."""
    return OutcomeEvalResult(outcome_id=run_id, measured=False)
