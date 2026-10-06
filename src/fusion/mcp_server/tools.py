"""MCP tool handlers."""

from __future__ import annotations

import os
import sys
from typing import Any

from fusion.config.env import is_test_mode
from fusion.mcp_server.schemas import (
    CompareClaudeRunsInput,
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
    Settings,
    build_pipelines,
    build_provider_registry,
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
from fusion.providers.base import close_providers
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


SOFT_TIMEOUT_ENV = "FUSION_TOOL_SOFT_TIMEOUT_S"
DEFAULT_SOFT_TIMEOUT_S = 90.0


def soft_timeout_from_env() -> float | None:
    """Seconds a tool call may take before it returns the panel's digest; None disables it.

    ``FUSION_TOOL_SOFT_TIMEOUT_S`` sets it (default 90); 0, ``off`` or ``none`` turn it off. An
    unusable value is reported on stderr and the default applies.
    """
    raw = os.environ.get(SOFT_TIMEOUT_ENV, "").strip().lower()
    if not raw:
        return DEFAULT_SOFT_TIMEOUT_S
    if raw in {"0", "off", "none", "false"}:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if value < 0:
        print(
            f"fusion: ignoring {SOFT_TIMEOUT_ENV}={raw!r} (use seconds, or 0 to turn it off); "
            f"using {DEFAULT_SOFT_TIMEOUT_S:.0f}",
            file=sys.stderr,
        )
        return DEFAULT_SOFT_TIMEOUT_S
    return value or None


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
        use_mock: bool | None = None,
    ) -> None:
        use_mock = is_test_mode(use_mock)
        providers = build_provider_registry(use_mock=use_mock)
        self.providers = providers
        self.run_store = RunStore(db_path=db_path)
        pipelines = build_pipelines(
            Settings(use_mock=use_mock, run_store=self.run_store), providers
        )
        self._code_review = code_review or pipelines["code_review"]
        self._ask = ask or pipelines["ask"]
        self._debug = debug or pipelines["debug"]
        self._architecture = architecture or pipelines["architecture"]
        self._plan = plan or pipelines["plan"]
        self._answer_eval = answer_eval or pipelines["answer_eval"]
        self._db_path = db_path
        self._use_mock = use_mock
        soft_timeout = soft_timeout_from_env()
        for pipeline in (
            self._code_review,
            self._ask,
            self._debug,
            self._architecture,
            self._plan,
            self._answer_eval,
        ):
            pipeline.soft_timeout_s = soft_timeout

    async def aclose(self) -> None:
        """Close provider HTTP clients and the run database; call when the host shuts down."""
        await close_providers(self.providers)
        self.run_store.close()

    async def fusion_ask(self, input: FusionAskInput) -> dict[str, Any]:
        """Answer a general coding task using Fusion as a model-like panel."""
        result = await self._ask.ask(
            PipelineFusionAskInput(
                prompt=input.prompt,
                context=input.context,
                file_snippets=input.file_snippets,
                changed_files=input.changed_files,
                budget=BudgetLevel(input.budget),
                strategy=input.strategy,
                max_cost_usd=input.max_cost_usd,
                detail=input.detail,
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
                repo_context=input.context,
                file_snippets=input.file_snippets,
                goals=input.goals,
                budget=BudgetLevel(input.budget),
                strategy=input.strategy,
                max_cost_usd=input.max_cost_usd,
                detail=input.detail,
                max_models=input.max_models,
                include_raw_outputs=input.include_raw_outputs,
                shadow_baseline=input.shadow_baseline,
            )
        )
        return result.model_dump()

    async def fusion_debug_error(self, input: DebugErrorInput) -> dict[str, Any]:
        """Debug an error using multi-model orchestration."""
        error = input.error_message
        if input.stack_trace:
            error += f"\n\nStack trace:\n{input.stack_trace}"
        result = await self._debug.debug(
            PipelineDebugInput(
                error_message=error,
                logs=input.logs,
                code_context=input.context,
                file_snippets=input.file_snippets,
                recent_changes=input.recent_changes,
                environment=input.environment,
                budget=BudgetLevel(input.budget),
                strategy=input.strategy,
                max_cost_usd=input.max_cost_usd,
                detail=input.detail,
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
                file_snippets=input.file_snippets,
                budget=BudgetLevel(input.budget),
                strategy=input.strategy,
                max_cost_usd=input.max_cost_usd,
                detail=input.detail,
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
                file_snippets=input.file_snippets,
                budget=BudgetLevel(input.budget),
                strategy=input.strategy,
                max_cost_usd=input.max_cost_usd,
                detail=input.detail,
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
                detail=input.detail,
            )
        )
        return result.model_dump()

    async def fusion_stats(self, input: FusionStatsInput) -> dict[str, Any]:
        """Return cumulative Fusion cost, latency, and shadow win-rate statistics."""
        stats = await self.run_store.aget_stats()
        recent = self.run_store.list_shadow_comparisons(limit=input.recent_shadow_limit)
        return {
            "display_markdown": format_stats_markdown(stats, recent),
            "result": stats_to_dict(stats, recent),
            "warnings": [],
        }

    def run_record(self, run_id: str) -> dict[str, Any]:
        """One stored run: its answer, claims, per-call cost and warnings (not the input text)."""
        record = self.run_store.get_run(run_id)
        if record is None:
            msg = f"No run '{run_id}'. Run ids come from the run_id field of a tool response."
            raise ValueError(msg)
        return {
            "run_id": record.run_id,
            "task_type": record.task_type,
            "status": record.status,
            "total_cost_usd": record.total_cost_usd,
            "total_latency_ms": record.total_latency_ms,
            "warnings": record.warnings,
            "routing": record.routing,
            "steps": [
                {
                    "step": s.step_name,
                    "model": s.model_name,
                    "provider": s.provider,
                    "input_tokens": s.input_tokens,
                    "output_tokens": s.output_tokens,
                    "cost_usd": s.cost_usd,
                    "latency_ms": s.latency_ms,
                }
                for s in record.steps
            ],
            "output": record.output_data,
        }

    def strategies_overview(self) -> dict[str, Any]:
        """The strategies a call may name, and which one each budget preset means."""
        book = self._code_review.deps.routing.strategies
        return {
            "budget_presets": dict(book.budget_map),
            "strategies": [
                {
                    "name": s.name,
                    "kind": s.kind,
                    "description": s.description,
                    "models": [m.model for m in s.members],
                    "rounds": s.rounds,
                    "aggregator": s.aggregator,
                    "aggregator_model": s.aggregator_model,
                    "judge": s.judge,
                    "max_cost_usd": s.max_cost_usd,
                    "max_latency_s": s.max_latency_s,
                }
                for s in (book.get(name) for name in book.names())
            ],
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
