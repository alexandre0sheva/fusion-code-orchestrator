"""Disagreement analysis between panel responses."""

from __future__ import annotations

from fusion.evals.schemas import ModelResponseEval


def compute_disagreement_score(evaluations: list[ModelResponseEval]) -> float:
    """Compute disagreement score (0=consensus, 1=high disagreement)."""
    if len(evaluations) < 2:
        return 0.0

    scores = [e.overall_score for e in evaluations]
    mean = sum(scores) / len(scores)
    variance = sum((s - mean) ** 2 for s in scores) / len(scores)
    return min(1.0, variance * 4.0)


def identify_outliers(
    evaluations: list[ModelResponseEval],
    threshold: float = 0.2,
) -> list[str]:
    """Identify model names whose scores deviate significantly from the mean."""
    if len(evaluations) < 2:
        return []

    scores = [e.overall_score for e in evaluations]
    mean = sum(scores) / len(scores)
    return [
        e.model_name
        for e in evaluations
        if abs(e.overall_score - mean) > threshold
    ]
