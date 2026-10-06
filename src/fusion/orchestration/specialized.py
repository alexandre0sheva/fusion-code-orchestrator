"""Task-specific pipelines: each builds its prompt context and maps the result to a tool output."""

from __future__ import annotations

from typing import TypedDict

from fusion.orchestration.context import PipelineContext
from fusion.orchestration.pipeline import BasePipeline
from fusion.orchestration.schemas import (
    AnswerEvalInput,
    AnswerEvalOutput,
    ArchitectureDecisionInput,
    ArchitectureDecisionOutput,
    CodeReviewInput,
    CodeReviewOutput,
    DebugInput,
    DebugOutput,
    FusionAskInput,
    FusionAskOutput,
    ImplementationPlanInput,
    ImplementationPlanOutput,
    PipelineEvals,
)
from fusion.routing.classifier import TaskType


class CodeReviewPipeline(BasePipeline):
    """Multi-model code review with structured findings."""

    task_type = TaskType.CODE_REVIEW

    async def review(self, input: CodeReviewInput) -> CodeReviewOutput:
        context_parts = []
        if input.repo_context:
            context_parts.append(input.repo_context)
        if input.goals:
            context_parts.append(f"Review goals:\n{input.goals}")
        ctx = PipelineContext(
            task_type=TaskType.CODE_REVIEW,
            primary_content=input.diff,
            context="\n\n".join(context_parts),
            changed_files=input.changed_files,
            file_snippets=input.file_snippets,
            budget=input.budget,
            strategy=input.strategy,
            max_cost_usd=input.max_cost_usd,
            max_models=input.max_models,
            shadow_baseline=input.shadow_baseline,
        )
        result = await self.run(ctx)
        structured = result.structured_output
        raw = None
        if input.include_raw_outputs:
            raw = [
                {"model": p.model_name, "content": p.content, "eval": p.evaluation.model_dump()}
                for p in result.panel_results
            ]
        common = self._common_output_fields(result, "Fusion Review", input.detail)
        return CodeReviewOutput(
            summary=str(structured.get("summary", result.final_answer[:500])),
            critical_findings=list(structured.get("critical_findings", [])),
            recommended_changes=list(structured.get("recommended_changes", [])),
            false_positive_risks=list(structured.get("false_positive_risks", [])),
            test_plan=list(structured.get("test_plan", [])),
            consensus=list(structured.get("consensus", [])),
            disagreements=list(structured.get("disagreements", [])),
            unique_insights=list(structured.get("unique_insights", [])),
            confidence=float(structured.get("confidence", result.final_eval.confidence)),
            evals=result.evals or PipelineEvals(),
            routing=result.routing,
            cost_latency=self._build_cost_latency(result),
            **common,
            run_id=result.run_id,
            raw_outputs=raw,
        )


class FusionAskPipeline(BasePipeline):
    """General model-like Fusion answer pipeline."""

    task_type = TaskType.DEFAULT

    async def ask(self, input: FusionAskInput) -> FusionAskOutput:
        ctx = PipelineContext(
            task_type=TaskType.DEFAULT,
            primary_content=input.prompt,
            context=input.context,
            file_snippets=input.file_snippets,
            changed_files=input.changed_files,
            budget=input.budget,
            strategy=input.strategy,
            max_cost_usd=input.max_cost_usd,
            max_models=input.max_models,
            shadow_baseline=input.shadow_baseline,
        )
        result = await self.run(ctx)
        s = result.structured_output
        raw = None
        if input.include_raw_outputs:
            raw = [
                {"model": p.model_name, "content": p.content, "eval": p.evaluation.model_dump()}
                for p in result.panel_results
            ]
        common = self._common_output_fields(result, "Fusion Answer", input.detail)
        answer = str(s.get("answer") or result.final_answer)
        return FusionAskOutput(
            answer=answer,
            summary=str(s.get("summary", answer[:500])),
            suggested_actions=list(s.get("suggested_actions", [])),
            tests_to_run=list(s.get("tests_to_run", [])),
            risks=list(s.get("risks", [])),
            assumptions=list(s.get("assumptions", [])),
            confidence=float(s.get("confidence", result.final_eval.confidence)),
            evals=result.evals or PipelineEvals(),
            routing=result.routing,
            cost_latency=self._build_cost_latency(result),
            **common,
            run_id=result.run_id,
            raw_outputs=raw,
        )


class DebugPipeline(BasePipeline):
    """Multi-model debug analysis pipeline."""

    task_type = TaskType.DEBUGGING

    async def debug(self, input: DebugInput) -> DebugOutput:
        primary = f"Error: {input.error_message}"
        if input.logs:
            primary += f"\n\nLogs:\n{input.logs}"
        context_parts = []
        if input.code_context:
            context_parts.append(f"Code context:\n{input.code_context}")
        if input.recent_changes:
            context_parts.append(f"Recent changes:\n{input.recent_changes}")
        if input.environment:
            context_parts.append(f"Environment:\n{input.environment}")
        ctx = PipelineContext(
            task_type=TaskType.DEBUGGING,
            primary_content=primary,
            context="\n\n".join(context_parts),
            file_snippets=input.file_snippets,
            budget=input.budget,
            strategy=input.strategy,
            max_cost_usd=input.max_cost_usd,
            shadow_baseline=input.shadow_baseline,
        )
        result = await self.run(ctx)
        s = result.structured_output
        common = self._common_output_fields(result, "Fusion Debug", input.detail)
        return DebugOutput(
            most_likely_causes=list(s.get("most_likely_causes", [])),
            ranked_hypotheses=list(s.get("ranked_hypotheses", [])),
            verification_steps=list(s.get("verification_steps", [])),
            minimal_fix_strategy=str(s.get("minimal_fix_strategy", "")),
            what_not_to_do=list(s.get("what_not_to_do", [])),
            confidence=float(s.get("confidence", result.final_eval.confidence)),
            evals=result.evals or PipelineEvals(),
            cost_latency=self._build_cost_latency(result),
            routing=result.routing,
            **common,
            run_id=result.run_id,
        )


class ArchitectureDecisionPipeline(BasePipeline):
    """Architecture decision support pipeline."""

    task_type = TaskType.ARCHITECTURE_DECISION

    async def decide(self, input: ArchitectureDecisionInput) -> ArchitectureDecisionOutput:
        primary = f"Decision question: {input.decision_question}"
        if input.options:
            primary += "\n\nOptions:\n" + "\n".join(f"- {o}" for o in input.options)
        if input.constraints:
            primary += f"\n\nConstraints:\n{input.constraints}"
        ctx = PipelineContext(
            task_type=TaskType.ARCHITECTURE_DECISION,
            primary_content=primary,
            context=input.repo_context,
            file_snippets=input.file_snippets,
            budget=input.budget,
            strategy=input.strategy,
            max_cost_usd=input.max_cost_usd,
            shadow_baseline=input.shadow_baseline,
        )
        result = await self.run(ctx)
        s = result.structured_output
        common = self._common_output_fields(result, "Fusion Architecture Decision", input.detail)
        return ArchitectureDecisionOutput(
            recommended_option=str(s.get("recommended_option", "")),
            tradeoffs=list(s.get("tradeoffs", [])),
            rejected_options=list(s.get("rejected_options", [])),
            risks=list(s.get("risks", [])),
            reversibility=str(s.get("reversibility", "")),
            migration_plan=list(s.get("migration_plan", [])),
            test_strategy=list(s.get("test_strategy", [])),
            confidence=float(s.get("confidence", result.final_eval.confidence)),
            evals=result.evals or PipelineEvals(),
            cost_latency=self._build_cost_latency(result),
            routing=result.routing,
            **common,
            run_id=result.run_id,
        )


class ImplementationPlanPipeline(BasePipeline):
    """Implementation planning pipeline."""

    task_type = TaskType.IMPLEMENTATION_PLAN

    async def plan(self, input: ImplementationPlanInput) -> ImplementationPlanOutput:
        primary = f"Feature request: {input.feature_request}"
        if input.constraints:
            primary += f"\n\nConstraints:\n{input.constraints}"
        if input.existing_patterns:
            primary += f"\n\nExisting patterns:\n{input.existing_patterns}"
        ctx = PipelineContext(
            task_type=TaskType.IMPLEMENTATION_PLAN,
            primary_content=primary,
            context=input.repo_context,
            file_snippets=input.file_snippets,
            budget=input.budget,
            strategy=input.strategy,
            max_cost_usd=input.max_cost_usd,
            shadow_baseline=input.shadow_baseline,
        )
        result = await self.run(ctx)
        s = result.structured_output
        common = self._common_output_fields(result, "Fusion Implementation Plan", input.detail)
        return ImplementationPlanOutput(
            implementation_sequence=list(s.get("implementation_sequence", [])),
            affected_modules=list(s.get("affected_modules", [])),
            data_model_changes=list(s.get("data_model_changes", [])),
            api_changes=list(s.get("api_changes", [])),
            ui_changes=list(s.get("ui_changes", [])),
            tests_to_add=list(s.get("tests_to_add", [])),
            risks=list(s.get("risks", [])),
            open_questions=list(s.get("open_questions", [])),
            confidence=float(s.get("confidence", result.final_eval.confidence)),
            evals=result.evals or PipelineEvals(),
            cost_latency=self._build_cost_latency(result),
            routing=result.routing,
            **common,
            run_id=result.run_id,
        )


class AnswerEvalPipeline(BasePipeline):
    """Answer quality evaluation pipeline."""

    task_type = TaskType.ANSWER_EVAL

    async def evaluate(self, input: AnswerEvalInput) -> AnswerEvalOutput:
        primary = f"Question: {input.question}\n\nAnswer to evaluate:\n{input.answer}"
        if input.rubric:
            primary += f"\n\nRubric:\n{input.rubric}"
        ctx = PipelineContext(
            task_type=TaskType.ANSWER_EVAL,
            primary_content=primary,
            context=input.context,
        )
        result = await self.run(ctx)
        s = result.structured_output
        common = self._common_output_fields(result, "Fusion Answer Evaluation", input.detail)
        return AnswerEvalOutput(
            score=float(s.get("score", result.final_eval.overall_score)),
            strengths=list(s.get("strengths", [])),
            weaknesses=list(s.get("weaknesses", [])),
            unsupported_claims=list(s.get("unsupported_claims", [])),
            missing_points=list(s.get("missing_points", [])),
            safer_answer=str(s.get("safer_answer", "")),
            confidence=float(s.get("confidence", result.final_eval.confidence)),
            evals=result.evals or PipelineEvals(),
            cost_latency=self._build_cost_latency(result),
            routing=result.routing,
            **common,
            run_id=result.run_id,
        )


class PipelineMap(TypedDict):
    """Concrete specialized pipelines keyed by MCP tool family."""

    ask: FusionAskPipeline
    code_review: CodeReviewPipeline
    debug: DebugPipeline
    architecture: ArchitectureDecisionPipeline
    plan: ImplementationPlanPipeline
    answer_eval: AnswerEvalPipeline
