"""Evaluate individual model responses."""

from __future__ import annotations

from fusion.evals.deterministic import run_deterministic_checks
from fusion.evals.llm_judge import heuristic_judge_scores
from fusion.evals.schemas import ModelResponseEval


def build_model_response_eval(
    *,
    model_name: str,
    content: str,
    judge_scores: dict[str, float | str] | None = None,
    is_judge_response: bool = False,
    is_coding_task: bool = False,
    known_files: list[str] | None = None,
) -> ModelResponseEval:
    """Build a ModelResponseEval from judge scores and deterministic checks."""
    scores = judge_scores or heuristic_judge_scores(content)
    det_passed, det_issues = run_deterministic_checks(
        content,
        is_judge=is_judge_response,
        is_coding_task=is_coding_task,
        min_length=30 if is_judge_response else 50,
        known_files=known_files,
    )

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
