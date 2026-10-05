"""Evaluate individual model responses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fusion.evals.deterministic import check_claims_include_tests, run_deterministic_checks
from fusion.evals.schemas import ModelResponseEval
from fusion.evals.structural import structural_scores

if TYPE_CHECKING:
    from fusion.orchestration.claims import PanelAnswer


def build_model_response_eval(
    *,
    model_name: str,
    content: str,
    judge_scores: dict[str, float | str] | None = None,
    is_judge_response: bool = False,
    is_coding_task: bool = False,
    known_files: list[str] | None = None,
    answer: PanelAnswer | None = None,
    structured: bool = False,
) -> ModelResponseEval:
    """Build a ModelResponseEval from judge scores (or claim-derived scores) and safety checks."""
    scores = judge_scores or structural_scores(
        answer, structured=structured, known_files=known_files
    )
    claims = answer.claims if answer is not None and structured else []
    det_passed, det_issues = run_deterministic_checks(
        content,
        is_judge=is_judge_response,
        min_length=30 if is_judge_response else 50,
        known_files=known_files,
        structured=structured,
        claim_files=[c.file for c in claims if c.file],
    )
    if structured:
        ok, issues = check_claims_include_tests(
            (c.kind for c in claims), is_coding_task=is_coding_task
        )
        if not ok:
            det_passed = False
            det_issues.extend(issues)

    def _float(key: str, default: float = 0.5) -> float:
        val = scores.get(key, default)
        return float(val) if isinstance(val, (int, float)) else default

    return ModelResponseEval(
        model_name=model_name,
        specificity=_float("specificity"),
        groundedness=_float("groundedness"),
        actionability=_float("actionability"),
        correctness_likelihood=_float("correctness_likelihood"),
        risk_awareness=_float("risk_awareness"),
        unsupported_claims=_float("unsupported_claims"),
        codebase_awareness=_float("codebase_awareness"),
        novelty=_float("novelty"),
        overall_score=_float("overall_score"),
        deterministic_passed=det_passed,
        deterministic_issues=det_issues,
        judge_notes=str(scores.get("notes", "")),
    )
