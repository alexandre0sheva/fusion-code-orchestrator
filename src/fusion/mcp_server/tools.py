"""MCP tool handlers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fusion.benchmark.compare import compare_implementations
from fusion.mcp_server.schemas import (
    CompareImplementInput,
    DebugErrorInput,
    DecideArchitectureInput,
    EvalAnswerInput,
    PlanFeatureInput,
    ReviewDiffInput,
)
from fusion.orchestration.pipelines import (
    AnswerEvalPipeline,
    ArchitectureDecisionPipeline,
    CodeReviewPipeline,
    DebugPipeline,
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
    ImplementationPlanInput as PipelinePlanInput,
)
from fusion.routing.budget import BudgetLevel


class FusionTools:
    """Handlers for fusion MCP tools."""

    def __init__(
        self,
        *,
        code_review: CodeReviewPipeline | None = None,
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
        self._debug = debug or pipelines["debug"]
        self._architecture = architecture or pipelines["architecture"]
        self._plan = plan or pipelines["plan"]
        self._answer_eval = answer_eval or pipelines["answer_eval"]
        self._db_path = db_path
        self._use_mock = use_mock

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
