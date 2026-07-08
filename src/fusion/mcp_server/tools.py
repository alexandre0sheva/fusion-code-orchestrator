"""MCP tool handlers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fusion.benchmark.compare import compare_implementations
from fusion.mcp_server.schemas import (
    CompareClaudeRunsInput,
    CompareImplementInput,
    DebugErrorInput,
    DecideArchitectureInput,
    EvalAnswerInput,
    FusionAskInput,
    FusionStatsInput,
    PlanFeatureInput,
    ReviewDiffInput,
)
from fusion.orchestration.pipelines import (
    AnswerEvalPipeline,
    ArchitectureDecisionPipeline,
    CodeReviewPipeline,
    DebugPipeline,
    FusionAskPipeline,
    ImplementationPlanPipeline,
    build_provider_registry,
    create_pipelines,
)
from fusion.orchestration.schemas import (
    AnswerEvalInput as PipelineAnswerEvalInput,
)
from fusion.orchestration.schemas import (
    ArchitectureDecisionInput as PipelineArchitectureInput,
)
from fusion.orchestration.schemas import (
    CodeReviewInput as PipelineCodeReviewInput,
)
from fusion.orchestration.schemas import (
    DebugInput as PipelineDebugInput,
)
from fusion.orchestration.schemas import (
    FusionAskInput as PipelineFusionAskInput,
)
from fusion.orchestration.schemas import (
    ImplementationPlanInput as PipelinePlanInput,
)
from fusion.routing.budget import BudgetLevel
from fusion.storage.run_store import RunStore
from fusion.telemetry.stats_format import format_stats_markdown, stats_to_dict


def _winner_from_delta(
    delta: float | int | None,
    opus_label: str,
    fusion_label: str,
) -> str:
    if delta is None:
        return "unknown"
    if abs(float(delta)) < 1e-9:
        return "tie"
    return fusion_label if delta < 0 else opus_label


def _format_compare_markdown(
    *,
    opus_label: str,
    fusion_label: str,
    better_arm: str,
    cheaper_arm: str,
    faster_arm: str,
    opus_score: float,
    fusion_score: float,
    cost_delta: float | None,
    latency_delta: int | None,
) -> str:
    cost_line = "unknown"
    if cost_delta is not None:
        direction = "cheaper" if cost_delta < 0 else "more expensive"
        cost_line = f"{fusion_label} is ${abs(cost_delta):.4f} {direction}"
    latency_line = "unknown"
    if latency_delta is not None:
        direction = "faster" if latency_delta < 0 else "slower"
        latency_line = f"{fusion_label} is {abs(latency_delta) / 1000:.1f}s {direction}"
    return "\n".join(
        [
            "## Claude Code Run Comparison",
            "",
            "### Verdict",
            f"- Better result: {better_arm}",
            f"- Cheaper: {cheaper_arm}",
            f"- Faster: {faster_arm}",
            "",
            "### Quality",
            f"- {opus_label}: {opus_score:.2f}",
            f"- {fusion_label}: {fusion_score:.2f}",
            "",
            "### Cost & latency",
            f"- Cost delta: {cost_line}",
            f"- Latency delta: {latency_line}",
            "",
            "### Note",
            (
                "- Claude Code executes both arms; Fusion only supplies panel reasoning "
                "and evaluation."
            ),
        ]
    )


class FusionTools:
    """Handlers for fusion MCP tools."""

    def __init__(
        self,
        *,
        code_review: CodeReviewPipeline | None = None,
        ask: FusionAskPipeline | None = None,
        debug: DebugPipeline | None = None,
        architecture: ArchitectureDecisionPipeline | None = None,
        plan: ImplementationPlanPipeline | None = None,
        answer_eval: AnswerEvalPipeline | None = None,
        db_path: str | None = None,
        use_mock: bool = False,
    ) -> None:
        providers = build_provider_registry(use_mock=use_mock)
        pipelines = create_pipelines(providers=providers, db_path=db_path)
        self._code_review = code_review or pipelines["code_review"]
        self._ask = ask or pipelines["ask"]
        self._debug = debug or pipelines["debug"]
        self._architecture = architecture or pipelines["architecture"]
        self._plan = plan or pipelines["plan"]
        self._answer_eval = answer_eval or pipelines["answer_eval"]
        self._db_path = db_path
        self._use_mock = use_mock

    async def fusion_ask(self, input: FusionAskInput) -> dict[str, Any]:
        """Answer a general coding task using Fusion as a model-like panel."""
        result = await self._ask.ask(
            PipelineFusionAskInput(
                prompt=input.prompt,
                context=input.context,
                file_snippets=input.file_snippets,
                changed_files=input.changed_files,
                budget=BudgetLevel(input.budget) if input.budget else BudgetLevel.MEDIUM,
                max_models=input.max_models,
                include_raw_outputs=input.include_raw_outputs,
                shadow_baseline=input.shadow_baseline,
            )
        )
        return result.model_dump()

    async def fusion_review_diff(self, input: ReviewDiffInput) -> dict[str, Any]:
        """Review a code diff using multi-model orchestration."""
        result = await self._code_review.review(
            PipelineCodeReviewInput(
                diff=input.diff,
                changed_files=input.changed_files,
                repo_context=input.repo_context or input.repo_summary,
                goals=input.goals,
                budget=BudgetLevel(input.budget) if input.budget else BudgetLevel.MEDIUM,
                max_models=input.max_models,
                include_raw_outputs=input.include_raw_outputs,
                shadow_baseline=input.shadow_baseline,
            )
        )
        return result.model_dump()

    async def fusion_debug_error(self, input: DebugErrorInput) -> dict[str, Any]:
        """Debug an error using multi-model orchestration."""
        result = await self._debug.debug(
            PipelineDebugInput(
                error_message=input.error_message,
                logs=input.logs,
                code_context=input.code_context or input.context,
                recent_changes=input.recent_changes,
                environment=input.environment,
                budget=BudgetLevel(input.budget) if input.budget else BudgetLevel.MEDIUM,
                shadow_baseline=input.shadow_baseline,
            )
        )
        return result.model_dump()

    async def fusion_decide_architecture(self, input: DecideArchitectureInput) -> dict[str, Any]:
        """Make an architecture decision using multi-model orchestration."""
        result = await self._architecture.decide(
            PipelineArchitectureInput(
                decision_question=input.question,
                constraints=input.constraints,
                options=input.options,
                repo_context=input.context,
                budget=BudgetLevel(input.budget) if input.budget else BudgetLevel.MEDIUM,
                shadow_baseline=input.shadow_baseline,
            )
        )
        return result.model_dump()

    async def fusion_plan_feature(self, input: PlanFeatureInput) -> dict[str, Any]:
        """Create an implementation plan using multi-model orchestration."""
        result = await self._plan.plan(
            PipelinePlanInput(
                feature_request=input.feature_description,
                constraints=input.constraints,
                repo_context=input.context,
                existing_patterns=input.existing_patterns,
                budget=BudgetLevel(input.budget) if input.budget else BudgetLevel.MEDIUM,
                shadow_baseline=input.shadow_baseline,
            )
        )
        return result.model_dump()

    async def fusion_eval_answer(self, input: EvalAnswerInput) -> dict[str, Any]:
        """Evaluate an answer using multi-model orchestration."""
        rubric = input.rubric
        if input.expected_criteria and not rubric:
            rubric = "\n".join(f"- {c}" for c in input.expected_criteria)
        result = await self._answer_eval.evaluate(
            PipelineAnswerEvalInput(
                question=input.question,
                answer=input.answer,
                context=input.context,
                rubric=rubric,
            )
        )
        return result.model_dump()

    async def fusion_stats(self, input: FusionStatsInput) -> dict[str, Any]:
        """Return cumulative Fusion cost, latency, and shadow win-rate statistics."""
        store = RunStore(db_path=self._db_path)
        stats = store.get_stats()
        recent = store.list_shadow_comparisons(limit=input.recent_shadow_limit)
        return {
            "display_markdown": format_stats_markdown(stats, recent),
            "result": stats_to_dict(stats, recent),
            "warnings": [],
        }

    async def fusion_compare_claude_runs(
        self,
        input: CompareClaudeRunsInput,
    ) -> dict[str, Any]:
        """Compare Claude Code + Opus output against Claude Code + Fusion output."""
        rubric = input.rubric or (
            "Score for correctness, groundedness in the provided context, Claude Code usefulness, "
            "testability, risk awareness, and minimal unsupported claims."
        )
        question = f"{input.task_prompt}\n\nRubric:\n{rubric}"
        if input.context:
            question += f"\n\nShared context / verification evidence:\n{input.context}"

        opus_eval = await self._answer_eval.evaluate(
            PipelineAnswerEvalInput(
                question=question,
                answer=input.opus_output,
                context=input.context,
                rubric=rubric,
            )
        )
        fusion_eval = await self._answer_eval.evaluate(
            PipelineAnswerEvalInput(
                question=question,
                answer=input.fusion_output,
                context=input.context,
                rubric=rubric,
            )
        )

        quality_delta = fusion_eval.score - opus_eval.score
        better_arm = (
            input.fusion_label
            if quality_delta > 0.02
            else input.opus_label
            if quality_delta < -0.02
            else "tie"
        )
        cost_delta = (
            input.fusion_cost_usd - input.opus_cost_usd
            if input.fusion_cost_usd is not None and input.opus_cost_usd is not None
            else None
        )
        latency_delta = (
            input.fusion_latency_ms - input.opus_latency_ms
            if input.fusion_latency_ms is not None and input.opus_latency_ms is not None
            else None
        )
        cheaper_arm = _winner_from_delta(cost_delta, input.opus_label, input.fusion_label)
        faster_arm = _winner_from_delta(latency_delta, input.opus_label, input.fusion_label)
        result = {
            "task_prompt": input.task_prompt,
            "better_arm": better_arm,
            "cheaper_arm": cheaper_arm,
            "faster_arm": faster_arm,
            "quality_delta": quality_delta,
            "cost_delta_usd": cost_delta,
            "latency_delta_ms": latency_delta,
            "opus": {
                "label": input.opus_label,
                "score": opus_eval.score,
                "confidence": opus_eval.confidence,
                "run_id": input.opus_run_id,
                "cost_usd": input.opus_cost_usd,
                "latency_ms": input.opus_latency_ms,
                "strengths": opus_eval.strengths,
                "weaknesses": opus_eval.weaknesses,
                "unsupported_claims": opus_eval.unsupported_claims,
                "eval_run_id": opus_eval.run_id,
            },
            "fusion": {
                "label": input.fusion_label,
                "score": fusion_eval.score,
                "confidence": fusion_eval.confidence,
                "run_id": input.fusion_run_id,
                "cost_usd": input.fusion_cost_usd,
                "latency_ms": input.fusion_latency_ms,
                "strengths": fusion_eval.strengths,
                "weaknesses": fusion_eval.weaknesses,
                "unsupported_claims": fusion_eval.unsupported_claims,
                "eval_run_id": fusion_eval.run_id,
            },
            "notes": [
                (
                    "Quality is evaluated by Fusion's answer-eval pipeline using the same "
                    "task and rubric."
                ),
                "Cost and latency winners require measured values from both arms.",
                "Claude Code remains the executor for both arms; this tool only compares outputs.",
            ],
        }
        if input.include_raw_evals:
            result["raw_evals"] = {
                "opus": opus_eval.model_dump(),
                "fusion": fusion_eval.model_dump(),
            }
        return {
            "display_markdown": _format_compare_markdown(
                opus_label=input.opus_label,
                fusion_label=input.fusion_label,
                better_arm=better_arm,
                cheaper_arm=cheaper_arm,
                faster_arm=faster_arm,
                opus_score=opus_eval.score,
                fusion_score=fusion_eval.score,
                cost_delta=cost_delta,
                latency_delta=latency_delta,
            ),
            "result": result,
            "evals": {
                "opus_eval_run_id": opus_eval.run_id,
                "fusion_eval_run_id": fusion_eval.run_id,
                "opus_score": opus_eval.score,
                "fusion_score": fusion_eval.score,
            },
            "warnings": [],
        }

    async def fusion_compare_implement(self, input: CompareImplementInput) -> dict[str, Any]:
        """Run Opus vs Fusion implementation benchmark with cost and latency."""
        root = input.workspace_root.strip() or os.environ.get(
            "FUSION_WORKSPACE_ROOT", os.getcwd()
        )
        result = await compare_implementations(
            task=input.task,
            workspace_root=Path(root),
            constraints=input.constraints,
            verify_command=input.verify_command,
            max_agent_steps=input.max_agent_steps,
            budget=BudgetLevel(input.budget) if input.budget else BudgetLevel.MEDIUM,
            opus_model=input.opus_model,
            fusion_executor_model=input.fusion_executor_model,
            db_path=self._db_path,
            use_mock=self._use_mock,
        )
        return result.model_dump()
