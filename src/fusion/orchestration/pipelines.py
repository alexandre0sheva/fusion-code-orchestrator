"""Orchestration pipeline implementation."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, TypedDict

from fusion.evals.engine import EvalEngine
from fusion.evals.schemas import (
    ContextEvalResult,
    FinalEvalResult,
    HybridEvalResult,
    ModelResponseEval,
    OutcomeEvalResult,
)
from fusion.orchestration.disagreement import analyze_disagreement
from fusion.orchestration.fanout import FanoutResult, fanout_to_panel
from fusion.orchestration.judge import judge_panel_responses
from fusion.orchestration.output_parser import parse_structured_output
from fusion.orchestration.schemas import (
    AnswerEvalInput,
    AnswerEvalOutput,
    ArchitectureDecisionInput,
    ArchitectureDecisionOutput,
    CodeReviewInput,
    CodeReviewOutput,
    CostLatencyInfo,
    DebugInput,
    DebugOutput,
    FusionAskInput,
    FusionAskOutput,
    ImplementationPlanInput,
    ImplementationPlanOutput,
    PipelineEvals,
    StepUsage,
)
from fusion.orchestration.synthesize import synthesize_responses
from fusion.providers.base import ModelProvider
from fusion.routing.budget import BudgetLevel, BudgetTracker
from fusion.routing.classifier import TaskClassifier, TaskType
from fusion.routing.model_registry import ModelRegistry
from fusion.routing.policy import RoutingDecision, RoutingPolicy
from fusion.security.policy import SecurityPolicy
from fusion.security.redaction import redact_secrets
from fusion.storage.run_store import RunStepRecord, RunStore
from fusion.telemetry.cost import (
    CostComparison,
    ModelUsage,
    PricingRegistry,
    UsageSummary,
    compare_to_baseline,
    model_usage_from_response,
)
from fusion.telemetry.traces import OrchestrationTrace, StepTrace


def _format_cost(amount: float | None, known: bool) -> str:
    if amount is None or not known:
        return "unknown"
    return f"${amount:.4f} estimated"


@dataclass
class PipelineContext:
    """Input context for a pipeline run."""

    task_type: TaskType
    primary_content: str
    context: str = ""
    file_snippets: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    budget: BudgetLevel = BudgetLevel.MEDIUM
    max_models: int | None = None


@dataclass
class PanelResult:
    """Result from a single panel model."""

    model_name: str
    provider: str
    provider_model_id: str
    content: str
    evaluation: ModelResponseEval
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    cost_known: bool = True
    cost_is_estimate: bool = True
    latency_ms: float = 0.0


@dataclass
class PipelineResult:
    """Complete result from an orchestration pipeline."""

    run_id: str
    task_type: str
    context_eval: ContextEvalResult
    panel_results: list[PanelResult]
    final_answer: str
    structured_output: dict[str, Any]
    final_eval: FinalEvalResult
    disagreement: dict[str, Any]
    routing: RoutingDecision
    trace: OrchestrationTrace
    total_cost_usd: float
    total_latency_ms: float
    usage: UsageSummary | None = None
    cost_comparison: CostComparison | None = None
    fanout: FanoutResult | None = None
    warnings: list[str] = field(default_factory=list)
    evals: PipelineEvals | None = None


class BasePipeline:
    """Shared multi-model orchestration engine."""

    task_type: TaskType = TaskType.DEFAULT

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        routing: RoutingPolicy,
        providers: dict[str, ModelProvider],
        eval_engine: EvalEngine,
        run_store: RunStore,
        security_policy: SecurityPolicy | None = None,
    ) -> None:
        self._registry = registry
        self._routing = routing
        self._providers = providers
        self._eval_engine = eval_engine
        self._run_store = run_store
        self._security = security_policy or SecurityPolicy.from_env()
        self._classifier = TaskClassifier()
        self._pricing = PricingRegistry()

    def _provider_available(self, model_alias: str) -> bool:
        entry = self._registry.get(model_alias)
        provider = self._providers.get(entry.provider)
        return provider is not None

    def _resolve_available_models(
        self,
        model_aliases: list[str],
        *,
        fallback: list[str],
        warnings: list[str],
        role: str,
    ) -> list[str]:
        available = [alias for alias in model_aliases if self._provider_available(alias)]
        if available:
            skipped = [alias for alias in model_aliases if alias not in available]
            if skipped:
                warnings.append(
                    f"Skipped {role} models with unavailable providers: {', '.join(skipped)}"
                )
            return available

        for alias in fallback:
            if self._provider_available(alias):
                warnings.append(f"No available {role} models; falling back to {alias}")
                return [alias]
        return model_aliases

    async def run(self, ctx: PipelineContext) -> PipelineResult:
        """Execute the full orchestration pipeline."""
        start = time.perf_counter()
        task_type = ctx.task_type
        warnings: list[str] = []

        sanitized_primary = redact_secrets(ctx.primary_content)
        sanitized_context = redact_secrets(ctx.context)
        sanitized_snippets = [redact_secrets(s).text for s in ctx.file_snippets]

        input_data = {
            "primary_content": ctx.primary_content,
            "context": ctx.context,
            "file_snippets": ctx.file_snippets,
            "changed_files": ctx.changed_files,
            "metadata": ctx.metadata,
            "budget": ctx.budget.value,
        }
        sanitized_input = {
            "primary_content": sanitized_primary.text,
            "context": sanitized_context.text,
            "file_snippets": sanitized_snippets,
            "changed_files": ctx.changed_files,
            "redaction_count": (
                sanitized_primary.redaction_count + sanitized_context.redaction_count
            ),
        }

        run_id = self._run_store.create_run(
            task_type=task_type.value,
            input_data=input_data,
            sanitized_input=sanitized_input,
        )

        trace = OrchestrationTrace(run_id=run_id, task_type=task_type.value)
        budget = BudgetTracker(config=self._routing.budgets.budgets)

        context_eval = self._eval_engine.evaluate_context(
            primary_content=sanitized_primary.text,
            context=sanitized_context.text,
            file_snippets=sanitized_snippets,
        )

        policy = self._routing.get_policy(task_type)
        routing_decision = self._routing.router.route(
            explicit_type=task_type.value,
            content=sanitized_primary.text,
            budget=ctx.budget,
        )
        if ctx.max_models:
            routing_decision.selected_panel = routing_decision.selected_panel[: ctx.max_models]

        warnings.extend(routing_decision.warnings)

        if context_eval.score < policy.min_context_score:
            final = self._eval_engine.evaluate_final(
                "Insufficient context provided.",
                is_coding_task=self._eval_engine.is_coding_task(task_type),
            )
            evals = self._build_evals(context_eval, [], {}, final, None, warnings)
            total_latency = (time.perf_counter() - start) * 1000
            usage = self._build_usage_summary(
                [],
                fusion_wall_latency_ms=total_latency,
                fanout=None,
                synthesis_latency_ms=None,
            )
            cost_comparison = compare_to_baseline(
                usage=usage,
                fusion_total_cost_usd=0.0,
                fusion_cost_known=True,
                pricing=self._pricing,
            )
            result = PipelineResult(
                run_id=run_id,
                task_type=task_type.value,
                context_eval=context_eval,
                panel_results=[],
                final_answer="Insufficient context for analysis.",
                structured_output={"summary": "Insufficient context for analysis."},
                final_eval=final,
                disagreement={"disagreement_score": 0.0, "consensus": True, "outlier_models": []},
                routing=routing_decision,
                trace=trace,
                total_cost_usd=0.0,
                total_latency_ms=total_latency,
                usage=usage,
                cost_comparison=cost_comparison,
                warnings=warnings,
                evals=evals,
            )
            self._persist_run(result, trace, [], routing_decision)
            return result

        panel_models = self._resolve_available_models(
            routing_decision.selected_panel,
            fallback=["gemini-flash", "claude-sonnet", "gpt-5.4-mini", "mock-fast"],
            warnings=warnings,
            role="panel",
        )
        judge_model = self._resolve_available_models(
            [routing_decision.judge_model],
            fallback=["gemini-flash", "claude-sonnet", "mock-judge"],
            warnings=warnings,
            role="judge",
        )[0]
        synthesizer_model = self._resolve_available_models(
            [routing_decision.synthesizer_model],
            fallback=["claude-sonnet", "gpt-5.4-mini", "gemini-flash", "mock-judge"],
            warnings=warnings,
            role="synthesizer",
        )[0]
        trace.panel_models = panel_models

        fanout_results = await fanout_to_panel(
            panel_models=panel_models,
            registry_models=self._registry.models,
            providers=self._providers,
            task_type=task_type,
            primary_content=sanitized_primary.text,
            context=sanitized_context.text,
            file_snippets=sanitized_snippets,
            changed_files=ctx.changed_files,
            config=self._routing.budgets.fanout,
        )

        warnings.extend(fanout_results.warnings)
        usage_models: list[ModelUsage] = []
        successful = fanout_results.successful
        fusion_cost_known = True
        fusion_total_cost = 0.0

        for call in fanout_results.calls:
            response = call.response
            if response is None:
                usage_models.append(
                    ModelUsage(
                        provider=call.provider,
                        model_alias=call.model_name,
                        provider_model_id=call.provider_model_id,
                        latency_ms=call.latency_ms,
                        success=False,
                        error_type=call.error_type,
                        error=call.error,
                    )
                )
                trace.add_step(
                    StepTrace(
                        step_name=f"panel:{call.model_name}",
                        model_name=call.model_name,
                        provider=call.provider,
                        latency_ms=call.latency_ms,
                        eval_summary={"error": call.error, "status": call.status},
                    )
                )
                continue

            entry = self._registry.get(call.model_name)
            cost = self._pricing.estimate_response_cost(response, entry)
            fusion_cost_known = fusion_cost_known and cost.known
            if cost.amount_usd is not None:
                fusion_total_cost += cost.amount_usd
                budget.record(cost_usd=cost.amount_usd, latency_ms=response.latency_ms)
            usage_models.append(
                model_usage_from_response(response, model_alias=call.model_name, cost=cost)
            )
            trace.add_step(
                StepTrace(
                    step_name=f"panel:{call.model_name}",
                    model_name=call.model_name,
                    provider=response.provider,
                    input_tokens=response.input_tokens or 0,
                    output_tokens=response.output_tokens or 0,
                    latency_ms=response.latency_ms,
                    cost_usd=cost.amount_usd or 0.0,
                    eval_summary={
                        "cost_known": cost.known,
                        "cost_is_estimate": cost.is_estimate,
                    },
                )
            )

        is_coding = self._eval_engine.is_coding_task(task_type)
        known_files = ctx.changed_files or None

        if not fanout_results.quorum_met:
            final_text = (
                "Fusion panel quorum was not met. "
                f"Only {fanout_results.success_count}/"
                f"{fanout_results.min_successful_responses} panel responses succeeded."
            )
            final = self._eval_engine.evaluate_final(final_text, is_coding_task=is_coding)
            disagreement = {
                "disagreement_score": 0.0,
                "consensus": False,
                "outlier_models": [],
                "quorum_met": False,
            }
            total_latency = (time.perf_counter() - start) * 1000
            usage = self._build_usage_summary(
                usage_models,
                fusion_wall_latency_ms=total_latency,
                fanout=fanout_results,
                synthesis_latency_ms=None,
            )
            cost_comparison = compare_to_baseline(
                usage=usage,
                fusion_total_cost_usd=fusion_total_cost if fusion_cost_known else None,
                fusion_cost_known=fusion_cost_known,
                pricing=self._pricing,
            )
            evals = self._build_evals(context_eval, [], disagreement, final, None, warnings)
            result = PipelineResult(
                run_id=run_id,
                task_type=task_type.value,
                context_eval=context_eval,
                panel_results=[],
                final_answer=final_text,
                structured_output={
                    "summary": final_text,
                    "quorum_met": False,
                    "failed_models": [
                        {
                            "model": call.model_name,
                            "status": call.status,
                            "error": call.error,
                        }
                        for call in fanout_results.calls
                        if not call.success
                    ],
                },
                final_eval=final,
                disagreement=disagreement,
                routing=routing_decision,
                trace=trace,
                total_cost_usd=fusion_total_cost,
                total_latency_ms=total_latency,
                usage=usage,
                cost_comparison=cost_comparison,
                fanout=fanout_results,
                warnings=warnings,
                evals=evals,
            )
            self._persist_run(result, trace, self._build_step_records(result), routing_decision)
            return result

        evaluations = await judge_panel_responses(
            eval_engine=self._eval_engine,
            responses=successful,
            task_type=task_type.value,
            judge_model=judge_model,
            context=sanitized_context.text,
            is_coding_task=is_coding,
            known_files=known_files,
        )

        judge_quality: HybridEvalResult | None = None
        if evaluations and self._eval_engine._use_llm_judge:
            first_ev = evaluations[0]
            judge_quality = await self._eval_engine.evaluate_judge_quality(
                judge_scores={
                    "overall_score": first_ev.overall_score,
                    "specificity": first_ev.specificity,
                    "groundedness": first_ev.groundedness,
                    "actionability": first_ev.actionability,
                },
                response_content=successful[0][1].content if successful else "",
            )
            if judge_quality.llm_judge_failed:
                warnings.append("LLM judge quality check failed; scores may be unreliable")

        panel_results: list[PanelResult] = []
        for (model_name, response), evaluation in zip(successful, evaluations, strict=False):
            entry = self._registry.get(model_name)
            cost = self._pricing.estimate_response_cost(response, entry)
            panel_results.append(
                PanelResult(
                    model_name=model_name,
                    provider=response.provider,
                    provider_model_id=response.model,
                    content=response.content,
                    evaluation=evaluation,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    cached_input_tokens=response.cached_input_tokens,
                    reasoning_tokens=response.reasoning_tokens,
                    cost_usd=cost.amount_usd,
                    cost_known=cost.known,
                    cost_is_estimate=cost.is_estimate,
                    latency_ms=response.latency_ms,
                )
            )

        panel_texts = [(m, r.content) for m, r in successful]
        disagreement = analyze_disagreement(
            evaluations,
            panel_contents=panel_texts,
        )
        raw_disagreement_score = disagreement.get("disagreement_score", 0.0)
        disagreement_score = (
            float(raw_disagreement_score)
            if isinstance(raw_disagreement_score, int | float | str)
            else 0.0
        )

        original_task = sanitized_primary.text
        synth_response = await synthesize_responses(
            synthesizer_model=synthesizer_model,
            registry_models=self._registry.models,
            providers=self._providers,
            task_type=task_type,
            panel_responses=panel_texts,
            disagreement_analysis=disagreement,
            original_task=original_task,
        )

        synth_entry = self._registry.get(synthesizer_model)
        synth_response.model_alias = synthesizer_model
        synth_cost = self._pricing.estimate_response_cost(synth_response, synth_entry)
        fusion_cost_known = fusion_cost_known and synth_cost.known
        if synth_cost.amount_usd is not None:
            fusion_total_cost += synth_cost.amount_usd
            budget.record(cost_usd=synth_cost.amount_usd, latency_ms=synth_response.latency_ms)
        usage_models.append(
            model_usage_from_response(
                synth_response,
                model_alias=synthesizer_model,
                cost=synth_cost,
            )
        )
        trace.add_step(
            StepTrace(
                step_name="synthesis",
                model_name=synthesizer_model,
                provider=synth_response.provider,
                input_tokens=synth_response.input_tokens or 0,
                output_tokens=synth_response.output_tokens or 0,
                latency_ms=synth_response.latency_ms,
                cost_usd=synth_cost.amount_usd or 0.0,
                eval_summary={
                    "cost_known": synth_cost.known,
                    "cost_is_estimate": synth_cost.is_estimate,
                },
            )
        )

        final_eval = self._eval_engine.evaluate_final(
            synth_response.content,
            is_coding_task=is_coding,
            known_files=known_files,
        )
        structured = parse_structured_output(
            task_type,
            synth_response.content,
            disagreement=disagreement,
            confidence=final_eval.confidence,
        )

        evals = self._build_evals(
            context_eval,
            evaluations,
            disagreement,
            final_eval,
            judge_quality,
            warnings,
        )

        trace.disagreement_score = disagreement_score
        warnings.extend(budget.warnings)
        total_latency = (time.perf_counter() - start) * 1000
        usage = self._build_usage_summary(
            usage_models,
            fusion_wall_latency_ms=total_latency,
            fanout=fanout_results,
            synthesis_latency_ms=round(synth_response.latency_ms),
        )
        cost_comparison = compare_to_baseline(
            usage=usage,
            fusion_total_cost_usd=fusion_total_cost if fusion_cost_known else None,
            fusion_cost_known=fusion_cost_known,
            pricing=self._pricing,
        )

        result = PipelineResult(
            run_id=run_id,
            task_type=task_type.value,
            context_eval=context_eval,
            panel_results=panel_results,
            final_answer=synth_response.content,
            structured_output=structured,
            final_eval=final_eval,
            disagreement=disagreement,
            routing=routing_decision,
            trace=trace,
            total_cost_usd=fusion_total_cost,
            total_latency_ms=total_latency,
            usage=usage,
            cost_comparison=cost_comparison,
            fanout=fanout_results,
            warnings=warnings,
            evals=evals,
        )

        steps = self._build_step_records(result)
        self._persist_run(result, trace, steps, routing_decision)
        return result

    def _build_evals(
        self,
        context_eval: ContextEvalResult,
        evaluations: list[ModelResponseEval],
        disagreement: dict[str, Any],
        final_eval: FinalEvalResult,
        judge_quality: HybridEvalResult | None,
        warnings: list[str],
    ) -> PipelineEvals:
        raw = self._eval_engine.build_pipeline_evals(
            context=context_eval,
            per_answer=evaluations,
            disagreement=disagreement,
            final=final_eval,
            judge_quality=judge_quality,
            outcome=OutcomeEvalResult(),
            warnings=warnings,
        )
        return PipelineEvals(**raw)

    def _build_step_records(self, result: PipelineResult) -> list[RunStepRecord]:
        steps: list[RunStepRecord] = []
        for pr in result.panel_results:
            steps.append(
                RunStepRecord(
                    step_name=f"panel:{pr.model_name}",
                    model_name=pr.model_name,
                    provider=pr.provider,
                    input_tokens=pr.input_tokens or 0,
                    output_tokens=pr.output_tokens or 0,
                    cost_usd=pr.cost_usd or 0.0,
                    latency_ms=pr.latency_ms,
                    eval_data={
                        **pr.evaluation.model_dump(),
                        "cost_known": pr.cost_known,
                        "cost_is_estimate": pr.cost_is_estimate,
                    },
                )
            )
        for usage in (result.usage.per_model if result.usage else []):
            step_name = f"model:{usage.model_alias or usage.provider_model_id}"
            if usage.success or any(s.model_name == usage.model_alias for s in steps):
                continue
            steps.append(
                RunStepRecord(
                    step_name=step_name,
                    model_name=usage.model_alias,
                    provider=usage.provider,
                    input_tokens=usage.input_tokens or 0,
                    output_tokens=usage.output_tokens or 0,
                    cost_usd=usage.estimated_cost_usd or usage.actual_cost_usd or 0.0,
                    latency_ms=usage.latency_ms,
                    eval_data={
                        "success": usage.success,
                        "error": usage.error,
                        "error_type": usage.error_type,
                        "cost_known": usage.cost_known,
                    },
                )
            )
        steps.append(
            RunStepRecord(
                step_name="final_eval",
                eval_data=result.final_eval.model_dump(),
            )
        )
        return steps

    def _build_cost_latency(self, result: PipelineResult) -> CostLatencyInfo:
        """Build token/cost breakdown for MCP and CLI consumers."""
        steps: list[StepUsage] = []
        total_in = result.usage.total_input_tokens if result.usage else 0
        total_out = result.usage.total_output_tokens if result.usage else 0
        total_cost_known = True

        for pr in result.panel_results:
            steps.append(
                StepUsage(
                    step_name=f"panel:{pr.model_name}",
                    model_name=pr.model_name,
                    provider=pr.provider,
                    input_tokens=pr.input_tokens or 0,
                    output_tokens=pr.output_tokens or 0,
                    cost_usd=pr.cost_usd,
                    cost_known=pr.cost_known,
                    cost_is_estimate=pr.cost_is_estimate,
                    latency_ms=pr.latency_ms,
                )
            )
            total_cost_known = total_cost_known and pr.cost_known

        for st in result.trace.steps:
            if st.step_name != "synthesis":
                continue
            steps.append(
                StepUsage(
                    step_name=st.step_name,
                    model_name=st.model_name,
                    provider=st.provider,
                    input_tokens=st.input_tokens,
                    output_tokens=st.output_tokens,
                    cost_usd=st.cost_usd,
                    cost_known=bool(st.eval_summary.get("cost_known", True)),
                    cost_is_estimate=bool(st.eval_summary.get("cost_is_estimate", True)),
                    latency_ms=st.latency_ms,
                )
            )
            total_cost_known = total_cost_known and bool(st.eval_summary.get("cost_known", True))

        usage_warnings = list(result.warnings)
        usage_warnings.append(
            "LLM judge calls add API cost not itemized in steps; Opus/Claude Code "
            "usage is billed separately and not visible to Fusion MCP."
        )

        return CostLatencyInfo(
            total_cost_usd=result.total_cost_usd if total_cost_known else None,
            total_cost_known=total_cost_known,
            total_latency_ms=result.total_latency_ms,
            total_input_tokens=total_in or 0,
            total_output_tokens=total_out or 0,
            steps=steps,
            warnings=usage_warnings,
        )

    def _build_usage_summary(
        self,
        per_model: list[ModelUsage],
        *,
        fusion_wall_latency_ms: float,
        fanout: FanoutResult | None,
        synthesis_latency_ms: int | None,
    ) -> UsageSummary:
        known_input = all(u.input_tokens is not None for u in per_model if u.success)
        known_output = all(u.output_tokens is not None for u in per_model if u.success)
        total_input = sum(u.input_tokens or 0 for u in per_model) if known_input else None
        total_output = sum(u.output_tokens or 0 for u in per_model) if known_output else None
        total_tokens = (
            (total_input or 0) + (total_output or 0)
            if total_input is not None and total_output is not None
            else None
        )
        return UsageSummary(
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_tokens=total_tokens,
            per_model=per_model,
            fusion_wall_latency_ms=round(fusion_wall_latency_ms),
            panel_wall_latency_ms=fanout.panel_wall_latency_ms if fanout else None,
            synthesis_latency_ms=synthesis_latency_ms,
            total_model_call_latency_ms=(
                (fanout.total_model_call_latency_ms if fanout else 0)
                + (synthesis_latency_ms or 0)
            ),
            max_panel_latency_ms=fanout.max_model_latency_ms if fanout else None,
            successful_model_calls=len([u for u in per_model if u.success]),
            failed_model_calls=len([u for u in per_model if not u.success]),
        )

    def _common_output_fields(self, result: PipelineResult, title: str) -> dict[str, Any]:
        usage = result.usage or self._build_usage_summary(
            [],
            fusion_wall_latency_ms=result.total_latency_ms,
            fanout=result.fanout,
            synthesis_latency_ms=None,
        )
        cost_comparison = result.cost_comparison or compare_to_baseline(
            usage=usage,
            fusion_total_cost_usd=result.total_cost_usd,
            fusion_cost_known=False,
            pricing=self._pricing,
        )
        return {
            "display_markdown": self._build_display_markdown(
                title=title,
                result=result,
                usage=usage,
                cost_comparison=cost_comparison,
            ),
            "result": result.structured_output,
            "usage": usage,
            "cost_comparison": cost_comparison,
            "warnings": result.warnings,
        }

    def _build_display_markdown(
        self,
        *,
        title: str,
        result: PipelineResult,
        usage: UsageSummary,
        cost_comparison: CostComparison,
    ) -> str:
        lines = [f"## {title}", "", "### Recommendation", result.final_answer.strip()[:1200]]
        lines.extend(["", "### Confidence", f"{result.final_eval.confidence:.2f}"])
        lines.extend(["", "### Cost & usage"])
        fusion_cost = _format_cost(
            cost_comparison.fusion_total_cost_usd,
            cost_comparison.fusion_cost_known,
        )
        baseline_cost = _format_cost(
            cost_comparison.baseline_estimated_cost_usd,
            cost_comparison.baseline_cost_known,
        )
        lines.append(f"- Fusion cost: {fusion_cost}")
        lines.append(
            f"- {cost_comparison.baseline_name} baseline estimate: "
            f"{baseline_cost}"
        )
        if cost_comparison.savings_usd is not None:
            label = "savings" if cost_comparison.fusion_is_cheaper else "extra cost"
            percent = (
                f" / {abs(cost_comparison.savings_percent):.1f}%"
                if cost_comparison.savings_percent is not None
                else ""
            )
            lines.append(
                f"- Estimated {label}: ${abs(cost_comparison.savings_usd):.4f}{percent}"
            )
        else:
            lines.append("- Estimated savings: unknown")
        panel_count = len(result.fanout.calls) if result.fanout else len(result.panel_results)
        succeeded = result.fanout.success_count if result.fanout else len(result.panel_results)
        failed = result.fanout.failed_count if result.fanout else 0
        lines.append(f"- Fusion wall time: {usage.fusion_wall_latency_ms / 1000:.1f}s")
        lines.append(f"- Panel: {panel_count} models, {succeeded} succeeded, {failed} failed")
        if cost_comparison.comparison_notes:
            lines.append(f"- Note: {cost_comparison.comparison_notes[0]}")
        if result.warnings:
            lines.extend(["", "### Caveats"])
            lines.extend(f"- {warning}" for warning in result.warnings[:5])
        return "\n".join(lines)

    def _persist_run(
        self,
        result: PipelineResult,
        trace: OrchestrationTrace,
        steps: list[RunStepRecord],
        routing: RoutingDecision,
    ) -> None:
        output = {
            "final_answer": result.final_answer,
            "structured_output": result.structured_output,
            "context_eval": result.context_eval.model_dump(),
            "panel_results": [
                {"model": p.model_name, "content": p.content, "eval": p.evaluation.model_dump()}
                for p in result.panel_results
            ],
            "final_eval": result.final_eval.model_dump(),
            "disagreement": result.disagreement,
            "routing": routing.model_dump(),
            "evals": result.evals.model_dump() if result.evals else {},
            "usage": result.usage.model_dump() if result.usage else {},
            "cost_comparison": (
                result.cost_comparison.model_dump() if result.cost_comparison else {}
            ),
            "fanout": result.fanout.model_dump() if result.fanout else {},
            "display_markdown": self._build_display_markdown(
                title="Fusion Result",
                result=result,
                usage=result.usage
                or self._build_usage_summary(
                    [],
                    fusion_wall_latency_ms=result.total_latency_ms,
                    fanout=result.fanout,
                    synthesis_latency_ms=None,
                ),
                cost_comparison=result.cost_comparison
                or compare_to_baseline(
                    usage=result.usage
                    or self._build_usage_summary(
                        [],
                        fusion_wall_latency_ms=result.total_latency_ms,
                        fanout=result.fanout,
                        synthesis_latency_ms=None,
                    ),
                    fusion_total_cost_usd=result.total_cost_usd,
                    fusion_cost_known=False,
                    pricing=self._pricing,
                ),
            ),
            "warnings": result.warnings,
        }
        self._run_store.complete_run(
            result.run_id,
            status="completed",
            output_data=output,
            trace=trace.model_dump(),
            routing=routing.model_dump(),
            warnings=result.warnings,
            total_cost_usd=result.total_cost_usd,
            total_latency_ms=result.total_latency_ms,
            steps=steps,
        )


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
            budget=input.budget,
            max_models=input.max_models,
        )
        result = await self.run(ctx)
        structured = result.structured_output
        raw = None
        if input.include_raw_outputs:
            raw = [
                {"model": p.model_name, "content": p.content, "eval": p.evaluation.model_dump()}
                for p in result.panel_results
            ]
        common = self._common_output_fields(result, "Fusion Review")
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
            max_models=input.max_models,
        )
        result = await self.run(ctx)
        s = result.structured_output
        raw = None
        if input.include_raw_outputs:
            raw = [
                {"model": p.model_name, "content": p.content, "eval": p.evaluation.model_dump()}
                for p in result.panel_results
            ]
        common = self._common_output_fields(result, "Fusion Answer")
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
            budget=input.budget,
        )
        result = await self.run(ctx)
        s = result.structured_output
        common = self._common_output_fields(result, "Fusion Debug")
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
            budget=input.budget,
        )
        result = await self.run(ctx)
        s = result.structured_output
        common = self._common_output_fields(result, "Fusion Architecture Decision")
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
            budget=input.budget,
        )
        result = await self.run(ctx)
        s = result.structured_output
        common = self._common_output_fields(result, "Fusion Implementation Plan")
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
        common = self._common_output_fields(result, "Fusion Answer Evaluation")
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


# Backward-compatible alias
OrchestrationPipeline = BasePipeline


class PipelineMap(TypedDict):
    """Concrete specialized pipelines keyed by MCP tool family."""

    ask: FusionAskPipeline
    code_review: CodeReviewPipeline
    debug: DebugPipeline
    architecture: ArchitectureDecisionPipeline
    plan: ImplementationPlanPipeline
    answer_eval: AnswerEvalPipeline


def create_pipeline(
    *,
    providers: dict[str, ModelProvider] | None = None,
    db_path: str | None = None,
    use_llm_judge: bool = True,
    use_mock: bool | None = None,
) -> BasePipeline:
    """Factory to create a configured pipeline with default dependencies."""
    from fusion.config.env import is_test_mode

    registry = ModelRegistry()
    routing = RoutingPolicy()
    resolved_providers = providers or build_provider_registry(use_mock=is_test_mode(use_mock))
    eval_engine = EvalEngine(
        registry=registry,
        provider_resolver=resolved_providers,
        use_llm_judge=use_llm_judge,
    )
    run_store = RunStore(db_path=db_path)
    return BasePipeline(
        registry=registry,
        routing=routing,
        providers=resolved_providers,
        eval_engine=eval_engine,
        run_store=run_store,
    )


def create_pipelines(
    *,
    providers: dict[str, ModelProvider] | None = None,
    db_path: str | None = None,
    use_llm_judge: bool = True,
    use_mock: bool | None = None,
) -> PipelineMap:
    """Create all specialized pipeline instances sharing dependencies."""
    from fusion.config.env import is_test_mode

    registry = ModelRegistry()
    routing = RoutingPolicy()
    resolved_providers = providers or build_provider_registry(use_mock=is_test_mode(use_mock))
    eval_engine = EvalEngine(
        registry=registry,
        provider_resolver=resolved_providers,
        use_llm_judge=use_llm_judge,
    )
    run_store = RunStore(db_path=db_path)
    return {
        "ask": FusionAskPipeline(
            registry=registry,
            routing=routing,
            providers=resolved_providers,
            eval_engine=eval_engine,
            run_store=run_store,
        ),
        "code_review": CodeReviewPipeline(
            registry=registry,
            routing=routing,
            providers=resolved_providers,
            eval_engine=eval_engine,
            run_store=run_store,
        ),
        "debug": DebugPipeline(
            registry=registry,
            routing=routing,
            providers=resolved_providers,
            eval_engine=eval_engine,
            run_store=run_store,
        ),
        "architecture": ArchitectureDecisionPipeline(
            registry=registry,
            routing=routing,
            providers=resolved_providers,
            eval_engine=eval_engine,
            run_store=run_store,
        ),
        "plan": ImplementationPlanPipeline(
            registry=registry,
            routing=routing,
            providers=resolved_providers,
            eval_engine=eval_engine,
            run_store=run_store,
        ),
        "answer_eval": AnswerEvalPipeline(
            registry=registry,
            routing=routing,
            providers=resolved_providers,
            eval_engine=eval_engine,
            run_store=run_store,
        ),
    }


def build_provider_registry(use_mock: bool = False) -> dict[str, ModelProvider]:
    """Build provider registry from environment."""
    from fusion.providers.anthropic import AnthropicProvider
    from fusion.providers.google import GoogleProvider
    from fusion.providers.lmstudio import LMStudioProvider
    from fusion.providers.mock import MockProvider
    from fusion.providers.ollama import OllamaProvider
    from fusion.providers.openai import OpenAIProvider

    providers: dict[str, ModelProvider] = {}
    if use_mock:
        providers["mock"] = MockProvider()

    provider_classes = (
        AnthropicProvider,
        OpenAIProvider,
        GoogleProvider,
        OllamaProvider,
        LMStudioProvider,
    )
    for cls in provider_classes:
        instance = cls()
        if instance.is_available():
            providers[instance.name] = instance

    if not use_mock and not any(
        name in providers for name in ("anthropic", "openai", "google")
    ):
        msg = (
            "No cloud providers configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
            "and/or GOOGLE_API_KEY in .env, or run with --mock for offline mode."
        )
        raise RuntimeError(msg)

    return providers
