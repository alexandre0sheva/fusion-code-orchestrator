"""Context sufficiency evaluation."""

from __future__ import annotations

from fusion.evals.schemas import ContextEvalResult


def evaluate_context(
    *,
    primary_content: str,
    context: str = "",
    file_snippets: list[str] | None = None,
    min_length: int = 20,
) -> ContextEvalResult:
    """Evaluate whether provided context is sufficient for the task."""
    missing: list[str] = []
    score = 0.0

    if len(primary_content.strip()) >= min_length:
        score += 0.4
    else:
        missing.append("Primary content is too short or empty")

    if context.strip():
        score += 0.3
    else:
        missing.append("No additional context provided")

    snippets = file_snippets or []
    if snippets:
        score += min(0.3, 0.1 * len(snippets))
    else:
        missing.append("No file snippets provided")

    score = min(1.0, score)
    return ContextEvalResult(
        sufficient=score >= 0.3,
        score=score,
        missing_items=missing,
        notes=f"Context score: {score:.2f}",
    )
