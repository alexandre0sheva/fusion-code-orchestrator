"""Final synthesis evaluation."""

from __future__ import annotations

from fusion.evals.deterministic import run_deterministic_checks
from fusion.evals.schemas import FinalEvalResult


def evaluate_final_answer(
    content: str,
    *,
    is_coding_task: bool = False,
    known_files: list[str] | None = None,
) -> FinalEvalResult:
    """Evaluate the final synthesized answer."""
    det_passed, det_issues = run_deterministic_checks(
        content,
        min_length=80,
        is_coding_task=is_coding_task,
        known_files=known_files,
        require_uncertainty=True,
    )

    has_recommendation = any(
        kw in content.lower() for kw in ("recommend", "should", "step", "phase", "fix")
    )
    has_confidence = "confidence" in content.lower() or "risk" in content.lower()
    has_test = "test" in content.lower()

    quality = 0.7 if has_recommendation else 0.4
    usefulness = 0.75 if has_recommendation else 0.45
    readiness = 0.65 if has_recommendation else 0.35
    test_quality = 0.7 if has_test else 0.3
    risk = 0.3 if has_confidence else 0.5
    confidence = 0.7 if has_confidence else 0.5

    overall = (quality + usefulness + readiness + test_quality + confidence) / 5.0

    return FinalEvalResult(
        final_answer_quality=quality,
        claude_code_usefulness=usefulness,
        implementation_readiness=readiness,
        test_plan_quality=test_quality,
        residual_risk=risk,
        confidence=confidence,
        overall_score=overall,
        deterministic_passed=det_passed,
        deterministic_issues=det_issues,
        notes="Heuristic final evaluation",
    )
