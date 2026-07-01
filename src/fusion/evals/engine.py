"""Central evaluation engine coordinating all eval types."""

from __future__ import annotations

from typing import Any

from fusion.evals.answer_eval import build_model_response_eval
from fusion.evals.context_eval import evaluate_context
from fusion.evals.deterministic import run_deterministic_checks
from fusion.evals.disagreement_eval import compute_disagreement_score, identify_outliers
from fusion.evals.final_eval import evaluate_final_answer
from fusion.evals.llm_judge import heuristic_judge_scores, judge_response
from fusion.evals.outcome_eval import evaluate_outcome
from fusion.evals.schemas import (
    ContextEvalResult,
    EvalDimension,
    FinalEvalResult,
    HybridEvalResult,
    ModelResponseEval,
    OutcomeEvalResult,
)
from fusion.providers.base import ModelProvider
from fusion.routing.classifier import TaskType, canonical_task_key
from fusion.routing.model_registry import ModelRegistry


class EvalEngine:
    """Runs LLM judge and deterministic evaluations with hybrid scoring."""

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        provider_resolver: dict[str, ModelProvider] | None = None,
        use_llm_judge: bool = True,
    ) -> None:
        self._registry = registry
        self._providers = provider_resolver or {}
        self._use_llm_judge = use_llm_judge

    def evaluate_context(
        self,
        *,
        primary_content: str,
        context: str = "",
        file_snippets: list[str] | None = None,
    ) -> ContextEvalResult:
        return evaluate_context(
            primary_content=primary_content,
            context=context,
            file_snippets=file_snippets,
        )

    async def evaluate_response(
        self,
        *,
        model_name: str,
        content: str,
        task_type: str,
        judge_model: str,
        context: str = "",
        known_files: list[str] | None = None,
        is_coding_task: bool = False,
    ) -> ModelResponseEval:
        judge_scores: dict[str, float | str] | None = None
        judge_failed = False
        if self._use_llm_judge:
            model_entry = self._registry.get(judge_model)
            provider = self._providers.get(model_entry.provider)
            if provider and provider.is_available():
                judge_scores = await judge_response(
                    provider=provider,
                    registry=self._registry,
                    judge_model=judge_model,
                    response_content=content,
                    task_type=task_type,
                    context=context,
                )
                notes = judge_scores.get("notes", "")
                if isinstance(notes, str) and notes.startswith("Failed"):
                    judge_failed = True
            else:
                judge_failed = True
        if judge_scores is None:
            judge_scores = heuristic_judge_scores(
                content,
                notes="LLM judge unavailable; using heuristic scoring",
            )

        is_coding = is_coding_task or task_type in {
            "code_review",
            "implementation_plan",
            "architecture_decision",
        }
        eval_result = build_model_response_eval(
            model_name=model_name,
            content=content,
            judge_scores=judge_scores,
            is_coding_task=is_coding,
            known_files=known_files,
        )
        if judge_failed:
            eval_result.judge_notes = (
                f"{eval_result.judge_notes}; LLM judge fallback active".strip("; ")
            )
        return eval_result

    async def evaluate_judge_quality(
        self,
        *,
        judge_scores: dict[str, Any],
        response_content: str,
    ) -> HybridEvalResult:
        """Evaluate whether the judge output is trustworthy."""
        warnings: list[str] = []
        dimensions: list[EvalDimension] = []

        det_passed, det_issues = run_deterministic_checks(
            str(judge_scores),
            is_judge=True,
            min_length=10,
            required_json_keys=["overall_score"],
        )
        if not det_passed:
            warnings.append("Judge output failed deterministic checks")

        score_keys = [
            "specificity",
            "groundedness",
            "actionability",
            "overall_score",
        ]
        for key in score_keys:
            val = judge_scores.get(key)
            if isinstance(val, (int, float)):
                dimensions.append(
                    EvalDimension(name=key, score=float(val), reason="From judge JSON")
                )

        if not dimensions:
            dimensions.append(
                EvalDimension(
                    name="judge_validity",
                    score=0.4,
                    reason="Judge returned no usable dimension scores",
                )
            )
            warnings.append("Judge quality low — scores may be unreliable")

        aggregate = sum(d.score for d in dimensions) / len(dimensions) if dimensions else 0.4
        return HybridEvalResult(
            dimensions=dimensions,
            aggregate_score=aggregate,
            llm_judge_used=True,
            llm_judge_failed=not det_passed,
            deterministic_passed=det_passed,
            deterministic_issues=det_issues,
            warnings=warnings,
            notes="Judge self-evaluation complete",
        )

    def evaluate_final(
        self,
        content: str,
        *,
        is_coding_task: bool = False,
        known_files: list[str] | None = None,
    ) -> FinalEvalResult:
        return evaluate_final_answer(
            content,
            is_coding_task=is_coding_task,
            known_files=known_files,
        )

    def evaluate_disagreement(
        self, evaluations: list[ModelResponseEval]
    ) -> tuple[float, list[str]]:
        return compute_disagreement_score(evaluations), identify_outliers(evaluations)

    def evaluate_outcome(self, run_id: str) -> OutcomeEvalResult:
        return evaluate_outcome(run_id=run_id)

    def build_pipeline_evals(
        self,
        *,
        context: ContextEvalResult | None,
        per_answer: list[ModelResponseEval],
        disagreement: dict[str, Any],
        final: FinalEvalResult | None,
        judge_quality: HybridEvalResult | None = None,
        outcome: OutcomeEvalResult | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        """Aggregate all eval results with visible dimension scores."""
        dimension_scores: dict[str, float] = {}
        if context:
            dimension_scores["context_sufficiency"] = context.score
        for ev in per_answer:
            dimension_scores[f"{ev.model_name}_overall"] = ev.overall_score
        if final:
            dimension_scores["final_quality"] = final.overall_score
            dimension_scores["confidence"] = final.confidence
        if judge_quality:
            dimension_scores["judge_quality"] = judge_quality.aggregate_score

        warning_list = warnings or []
        answer_quality = (
            sum(ev.overall_score for ev in per_answer) / len(per_answer) if per_answer else 0.0
        )
        context_score = context.score if context else 0.0
        final_score = final.overall_score if final else 0.0
        consensus_strength = 1.0 - float(disagreement.get("disagreement_score", 0.0) or 0.0)
        provider_failures = len(
            [w for w in warning_list if "Panel model" in w or "quorum" in w.lower()]
        )
        provider_success_rate = (
            len(per_answer) / (len(per_answer) + provider_failures)
            if per_answer or provider_failures
            else 1.0
        )
        unsupported_claim_penalty = (
            sum(ev.unsupported_claims for ev in per_answer) / len(per_answer) * 0.15
            if per_answer
            else 0.0
        )
        high_risk_penalty = (final.residual_risk * 0.10) if final else 0.0
        aggregate = (
            context_score * 0.20
            + consensus_strength * 0.20
            + answer_quality * 0.25
            + final_score * 0.25
            + provider_success_rate * 0.10
        )
        aggregate = max(0.0, min(1.0, aggregate - unsupported_claim_penalty - high_risk_penalty))
        dimension_scores.update(
            {
                "aggregate_context_sufficiency": context_score,
                "aggregate_consensus_strength": consensus_strength,
                "aggregate_answer_quality": answer_quality,
                "aggregate_final_quality": final_score,
                "aggregate_provider_success_rate": provider_success_rate,
                "aggregate_unsupported_claim_penalty": unsupported_claim_penalty,
                "aggregate_high_risk_penalty": high_risk_penalty,
            }
        )

        return {
            "context": context.model_dump() if context else None,
            "per_answer": [e.model_dump() for e in per_answer],
            "disagreement": disagreement,
            "judge_quality": judge_quality.model_dump() if judge_quality else None,
            "final": final.model_dump() if final else None,
            "outcome": (outcome or evaluate_outcome(run_id="")).model_dump(),
            "aggregate_score": aggregate,
            "dimension_scores": dimension_scores,
            "warnings": warnings or [],
        }

    def is_coding_task(self, task_type: TaskType) -> bool:
        return canonical_task_key(task_type) in {
            "code_review",
            "implementation_plan",
            "architecture_decision",
        }
