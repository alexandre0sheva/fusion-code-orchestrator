"""Pipeline stages. Each reads and writes only ``RunState``; nothing else is shared.

Order: Redact -> Route -> ContextEval -> Panel -> Refine -> Judge -> Aggregate -> FinalEval ->
Shadow -> Persist. A stage may halt the run (``state.halt``); every later stage is then skipped
except those marked ``always_runs`` (Persist), which still stores a diagnostic result.
"""

from __future__ import annotations

from typing import Protocol

from fusion.benchmark.shadow import run_shadow_comparison, should_run_shadow
from fusion.orchestration.context import Halt, PipelineDeps, RunState
from fusion.orchestration.disagreement import analyze_disagreement
from fusion.orchestration.fanout import fanout_to_panel
from fusion.orchestration.judge import judge_panel_responses
from fusion.orchestration.ledger import CallRecord
from fusion.orchestration.output import step_name
from fusion.orchestration.output_parser import parse_structured_output
from fusion.orchestration.prompts import build_user_prompt, get_system_prompt
from fusion.orchestration.refine import refine_panel_responses
from fusion.orchestration.result import PanelResult, PipelineResult, build_usage_summary
from fusion.orchestration.strategy import PanelMember
from fusion.orchestration.synthesize import build_digest, synthesize_responses
from fusion.security.redaction import redact_secrets
from fusion.storage.run_store import ShadowComparisonRecord
from fusion.telemetry.cost import CostComparison, compare_to_baseline
from fusion.telemetry.traces import OrchestrationTrace, StepTrace


class Stage(Protocol):
    always_runs: bool

    async def run(self, state: RunState) -> RunState: ...


class _Stage:
    always_runs = False

    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps


# --------------------------------------------------------------------------------------- redact


class RedactStage(_Stage):
    """Redact secrets from the input and open the run record."""

    async def run(self, state: RunState) -> RunState:
        ctx = state.ctx
        primary = redact_secrets(ctx.primary_content)
        context = redact_secrets(ctx.context)
        state.sanitized_primary = primary.text
        state.sanitized_context = context.text
        state.sanitized_snippets = [redact_secrets(s).text for s in ctx.file_snippets]
        state.redaction_count = primary.redaction_count + context.redaction_count

        state.run_id = await self.deps.run_store.acreate_run(
            task_type=ctx.task_type.value,
            input_data={
                "primary_content": ctx.primary_content,
                "context": ctx.context,
                "file_snippets": ctx.file_snippets,
                "changed_files": ctx.changed_files,
                "metadata": ctx.metadata,
                "budget": ctx.budget.value,
                "strategy": ctx.strategy,
                "mode": state.mode.value,
            },
            sanitized_input={
                "primary_content": state.sanitized_primary,
                "context": state.sanitized_context,
                "file_snippets": state.sanitized_snippets,
                "changed_files": ctx.changed_files,
                "redaction_count": state.redaction_count,
            },
        )
        state.trace = OrchestrationTrace(run_id=state.run_id, task_type=ctx.task_type.value)
        return state


# ---------------------------------------------------------------------------------------- route


class RouteStage(_Stage):
    """Apply the run's strategy: pick models, and resolve which are actually available."""

    async def run(self, state: RunState) -> RunState:
        ctx = state.ctx
        strategy = state.strategy
        assert strategy is not None  # resolved by RunState.start
        decision = self.deps.routing.router.route(
            strategy=strategy,
            explicit_type=ctx.task_type.value,
            content=state.sanitized_primary,
        )
        if ctx.max_models:
            decision.selected_panel = decision.selected_panel[: ctx.max_models]
        state.routing = decision
        state.warnings.extend(decision.warnings)

        state.panel_models = self._available(decision.selected_panel, "panel", state.warnings)
        configured = {m.model: m for m in strategy.members}
        state.members = [configured.get(a, PanelMember(model=a)) for a in state.panel_models]
        state.judge_model = self._available([decision.judge_model], "judge", state.warnings)[0]
        if decision.synthesizer_model:
            state.synthesizer_model = self._available(
                [decision.synthesizer_model], "synthesizer", state.warnings
            )[0]
        if state.trace is not None:
            state.trace.panel_models = state.panel_models
        return state

    def _provider_available(self, alias: str) -> bool:
        entry = self.deps.registry.get(alias)
        return entry.provider in self.deps.providers

    def _available(self, aliases: list[str], role: str, warnings: list[str]) -> list[str]:
        """Keep aliases whose provider is configured; else fall back to a model with that role."""
        available = [a for a in aliases if self._provider_available(a)]
        if available:
            skipped = [a for a in aliases if a not in available]
            if skipped:
                warnings.append(
                    f"Skipped {role} models with unavailable providers: {', '.join(skipped)}"
                )
            return available
        for alias in self.deps.registry.by_role(role):
            if self._provider_available(alias):
                warnings.append(f"No available {role} models; falling back to {alias}")
                return [alias]
        return aliases


# ------------------------------------------------------------------------------------ context


class ContextEvalStage(_Stage):
    """Score the supplied context; halt when there is too little to analyse."""

    async def run(self, state: RunState) -> RunState:
        engine = self.deps.eval_engine
        state.context_eval = engine.evaluate_context(
            primary_content=state.sanitized_primary,
            context=state.sanitized_context,
            file_snippets=state.sanitized_snippets,
        )
        policy = self.deps.routing.get_policy(state.task_type)
        if state.context_eval.score >= policy.min_context_score:
            return state

        text = "Insufficient context for analysis."
        state.final_answer = text
        state.structured = {"summary": text}
        state.final_eval = engine.evaluate_final(
            "Insufficient context provided.",
            is_coding_task=engine.is_coding_task(state.task_type),
        )
        state.disagreement = {"disagreement_score": 0.0, "consensus": True, "outlier_models": []}
        state.evals = self.deps.presenter.build_evals(
            state.context_eval, [], {}, state.final_eval, None, state.warnings
        )
        state.panel_models = []
        if state.trace is not None:
            state.trace.panel_models = []
        state.halt = Halt("insufficient_context")
        state.stamp_latency(self.deps.clock)
        return state


# ---------------------------------------------------------------------------------------- panel


class PanelStage(_Stage):
    """Fan the task out to the panel; halt when too few models answered."""

    async def run(self, state: RunState) -> RunState:
        fanout = await fanout_to_panel(
            panel_models=state.panel_models,
            registry_models=self.deps.registry.models,
            providers=self.deps.providers,
            task_type=state.task_type,
            primary_content=state.sanitized_primary,
            context=state.sanitized_context,
            file_snippets=state.sanitized_snippets,
            changed_files=state.ctx.changed_files,
            config=self.deps.routing.budgets.fanout,
            gateway=state.gateway,
            members={m.model: m for m in state.members},
        )
        state.fanout = fanout
        state.warnings.extend(fanout.warnings)
        state.successful = fanout.successful
        if not fanout.quorum_met:
            self._halt_without_quorum(state)
        return state

    def _halt_without_quorum(self, state: RunState) -> None:
        fanout = state.fanout
        assert fanout is not None and state.context_eval is not None  # set by earlier stages
        engine = self.deps.eval_engine
        text = (
            "Fusion panel quorum was not met. "
            f"Only {fanout.success_count}/{fanout.min_successful_responses} "
            "panel responses succeeded."
        )
        state.final_answer = text
        state.final_eval = engine.evaluate_final(
            text, is_coding_task=engine.is_coding_task(state.task_type)
        )
        state.disagreement = {
            "disagreement_score": 0.0,
            "consensus": False,
            "outlier_models": [],
            "quorum_met": False,
        }
        state.structured = {
            "summary": text,
            "quorum_met": False,
            "failed_models": [
                {"model": c.model_name, "status": c.status, "error": c.error}
                for c in fanout.calls
                if not c.success
            ],
        }
        state.evals = self.deps.presenter.build_evals(
            state.context_eval, [], state.disagreement, state.final_eval, None, state.warnings
        )
        state.halt = Halt("quorum")
        state.stamp_latency(self.deps.clock)


# --------------------------------------------------------------------------------------- refine


class RefineStage(_Stage):
    """Mixture-of-agents rounds: panelists revise after seeing anonymized peers.

    A strategy's ``rounds`` is the number of answer rounds, so ``rounds - 1`` refinements run.
    """

    async def run(self, state: RunState) -> RunState:
        assert state.strategy is not None
        config = self.deps.routing.budgets.refinement
        members = {m.model: m for m in state.members}
        for _ in range(state.strategy.rounds - 1):
            state.successful, round_result = await refine_panel_responses(
                responses=state.successful,
                registry_models=self.deps.registry.models,
                providers=self.deps.providers,
                task_type=state.task_type,
                original_task=state.sanitized_primary,
                config=config,
                gateway=state.gateway,
                members=members,
            )
            merged = round_result
            if state.refinement is not None:
                merged = state.refinement.merged(round_result)
            state.refinement = merged
            state.warnings.extend(round_result.warnings)
            if not round_result.ran:
                break
        return state


# ---------------------------------------------------------------------------------------- judge


class JudgeStage(_Stage):
    """Score each panel answer: deterministic checks always, the LLM judge per the strategy.

    ``judge: off`` makes no model call, ``light`` scores each answer with the judge model, and
    ``full`` also checks the judge's own output.
    """

    async def run(self, state: RunState) -> RunState:
        assert state.strategy is not None
        engine = self.deps.eval_engine
        judge = state.strategy.judge
        state.evaluations = await judge_panel_responses(
            eval_engine=engine,
            responses=state.successful,
            task_type=state.task_type.value,
            judge_model=state.judge_model,
            context=state.sanitized_context,
            is_coding_task=engine.is_coding_task(state.task_type),
            known_files=state.ctx.changed_files or None,
            gateway=state.gateway,
            use_llm=judge != "off",
        )
        if state.evaluations and engine.use_llm_judge and judge == "full":
            await self._check_judge_quality(state)
        state.panel_results = self._panel_results(state)
        return state

    async def _check_judge_quality(self, state: RunState) -> None:
        first = state.evaluations[0]
        state.judge_quality = await self.deps.eval_engine.evaluate_judge_quality(
            judge_scores={
                "overall_score": first.overall_score,
                "specificity": first.specificity,
                "groundedness": first.groundedness,
                "actionability": first.actionability,
            },
            response_content=state.successful[0][1].content if state.successful else "",
        )
        if state.judge_quality.llm_judge_failed:
            state.warnings.append("LLM judge quality check failed; scores may be unreliable")

    def _panel_results(self, state: RunState) -> list[PanelResult]:
        results: list[PanelResult] = []
        pairs = zip(state.successful, state.evaluations, strict=False)
        for (model_name, response), evaluation in pairs:
            entry = self.deps.registry.get(model_name)
            cost = self.deps.pricing.estimate_response_cost(response, entry)
            results.append(
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
        return results


# ------------------------------------------------------------------------------------ aggregate


class AggregateStage(_Stage):
    """Analyse agreement, then produce the final answer the way the strategy says.

    ``solo`` returns the one member's answer; a ``digest`` aggregator returns the panel's answers
    for Claude Code to merge; ``llm`` makes one synthesizer call.
    """

    async def run(self, state: RunState) -> RunState:
        assert state.strategy is not None
        strategy = state.strategy
        panel_texts = [(m, r.content) for m, r in state.successful]
        state.disagreement = analyze_disagreement(state.evaluations, panel_contents=panel_texts)
        if strategy.kind == "solo":
            state.synth_response = state.successful[0][1]
        elif strategy.aggregator == "digest":
            state.synth_response = build_digest(panel_texts, state.disagreement)
        else:
            state.synth_response = await synthesize_responses(
                synthesizer_model=state.synthesizer_model,
                registry_models=self.deps.registry.models,
                providers=self.deps.providers,
                task_type=state.task_type,
                panel_responses=panel_texts,
                disagreement_analysis=state.disagreement,
                original_task=state.sanitized_primary,
                gateway=state.gateway,
            )
        return state


# ----------------------------------------------------------------------------------- final eval


class FinalEvalStage(_Stage):
    """Evaluate and parse the final answer, then settle budget warnings and wall time."""

    async def run(self, state: RunState) -> RunState:
        engine = self.deps.eval_engine
        synth = state.synth_response
        assert synth is not None and state.context_eval is not None  # set by earlier stages
        state.final_answer = synth.content
        state.final_eval = engine.evaluate_final(
            synth.content,
            is_coding_task=engine.is_coding_task(state.task_type),
            known_files=state.ctx.changed_files or None,
        )
        state.structured = parse_structured_output(
            state.task_type,
            synth.content,
            disagreement=state.disagreement,
            confidence=state.final_eval.confidence,
        )
        state.evals = self.deps.presenter.build_evals(
            state.context_eval,
            state.evaluations,
            state.disagreement,
            state.final_eval,
            state.judge_quality,
            state.warnings,
        )
        if state.trace is not None:
            state.trace.disagreement_score = _score(state.disagreement)
        for record in state.ledger.ordered_records(state.panel_models):
            if record.cost_usd is not None:
                state.budget.record(cost_usd=record.cost_usd, latency_ms=record.latency_ms)
        state.warnings.extend(state.budget.warnings)
        state.stamp_latency(self.deps.clock)
        state.warnings.extend(_cap_warnings(state))
        return state


def _cap_warnings(state: RunState) -> list[str]:
    """Warn when a run went over its strategy's cost or latency cap (caps do not stop a run)."""
    strategy = state.strategy
    assert strategy is not None
    found: list[str] = []
    total = state.ledger.total_cost()
    if strategy.max_cost_usd is not None and total.known and total.usd > strategy.max_cost_usd:
        found.append(
            f"Strategy '{strategy.name}' cost ${total.usd:.4f} exceeded its cap "
            f"of ${strategy.max_cost_usd:.4f}"
        )
    seconds = state.total_latency_ms / 1000
    if strategy.max_latency_s is not None and seconds > strategy.max_latency_s:
        found.append(
            f"Strategy '{strategy.name}' took {seconds:.1f}s, over its cap "
            f"of {strategy.max_latency_s:.1f}s"
        )
    return found


def _score(disagreement: dict[str, object]) -> float:
    raw = disagreement.get("disagreement_score", 0.0)
    return float(raw) if isinstance(raw, int | float | str) else 0.0


# ---------------------------------------------------------------------------------------- shadow


class ShadowStage(_Stage):
    """Optional blind A/B against the real frontier baseline (measurement, not Fusion cost)."""

    async def run(self, state: RunState) -> RunState:
        if not state.mode_settings.shadow or not should_run_shadow(state.ctx.shadow_baseline):
            return state
        assert state.synth_response is not None
        fusion_cost = state.ledger.total_cost()
        ctx = state.ctx
        shadow = await run_shadow_comparison(
            task_prompt=build_user_prompt(
                task_type=state.task_type,
                primary_content=state.sanitized_primary,
                context=state.sanitized_context,
                file_snippets=state.sanitized_snippets,
                changed_files=ctx.changed_files,
            ),
            system_prompt=get_system_prompt(state.task_type),
            fusion_answer=state.synth_response.content,
            fusion_cost_usd=fusion_cost.usd if fusion_cost.known else None,
            fusion_latency_ms=state.total_latency_ms,
            registry_models=self.deps.registry.models,
            providers=self.deps.providers,
            judge_model_alias=state.judge_model,
            pricing=self.deps.pricing,
            baseline=self.deps.shadow_baseline,
            gateway=state.gateway,
        )
        state.shadow = shadow
        state.warnings.extend(shadow.warnings)
        if shadow.ran:
            await self._store(state)
        return state

    async def _store(self, state: RunState) -> None:
        shadow = state.shadow
        assert shadow is not None
        await self.deps.run_store.arecord_shadow_comparison(
            ShadowComparisonRecord(
                run_id=state.run_id,
                task_type=state.task_type.value,
                baseline_model=shadow.baseline_model,
                judge_model=shadow.judge_model,
                winner=shadow.winner,
                fusion_score=shadow.fusion_score,
                baseline_score=shadow.baseline_score,
                fusion_cost_usd=shadow.fusion_cost_usd,
                baseline_cost_usd=shadow.baseline_cost_usd,
                fusion_latency_ms=shadow.fusion_latency_ms,
                baseline_latency_ms=shadow.baseline_latency_ms,
                raw={
                    "baseline_name": shadow.baseline_name,
                    "judge_reason": shadow.judge_reason,
                    "warnings": shadow.warnings,
                },
            )
        )


# ---------------------------------------------------------------------------------------- persist


class PersistStage(_Stage):
    """Assemble the result from the ledger and state, and store the run."""

    always_runs = True

    async def run(self, state: RunState) -> RunState:
        result = self._build_result(state)
        presenter = self.deps.presenter
        await self.deps.run_store.acomplete_run(
            result.run_id,
            status="completed",
            output_data=presenter.persisted_output(result),
            trace=result.trace.model_dump(),
            routing=result.routing.model_dump(),
            warnings=result.warnings,
            total_cost_usd=result.total_cost_usd,
            total_latency_ms=result.total_latency_ms,
            steps=presenter.step_records(result),
        )
        state.result = result
        return state

    def _build_result(self, state: RunState) -> PipelineResult:
        assert state.trace is not None and state.routing is not None
        assert state.context_eval is not None and state.final_eval is not None
        total = state.ledger.total_cost()
        usage = build_usage_summary(
            state.ledger,
            model_order=state.panel_models,
            wall_latency_ms=state.total_latency_ms,
            fanout=state.fanout,
        )
        comparison = compare_to_baseline(
            usage=usage,
            fusion_total_cost_usd=total.usd if total.known else None,
            fusion_cost_known=total.known,
            pricing=self.deps.pricing,
        )
        comparison = _with_shadow_actuals(comparison, state, total.usd, total.known)
        trace = state.trace
        for record in state.ledger.ordered_records(state.panel_models):
            trace.add_step(_step_trace(record))
        return PipelineResult(
            run_id=state.run_id,
            task_type=state.task_type.value,
            context_eval=state.context_eval,
            panel_results=state.panel_results,
            final_answer=state.final_answer,
            structured_output=state.structured,
            final_eval=state.final_eval,
            disagreement=state.disagreement,
            routing=state.routing,
            trace=trace,
            total_cost_usd=total.usd,
            total_latency_ms=state.total_latency_ms,
            usage=usage,
            cost_comparison=comparison,
            fanout=state.fanout,
            refinement=state.refinement,
            shadow=state.shadow,
            warnings=state.warnings,
            evals=state.evals,
            ledger=state.ledger,
            mode=state.mode,
        )


def _step_trace(record: CallRecord) -> StepTrace:
    summary: dict[str, object] = {
        "cost_known": record.cost_known,
        "cost_is_estimate": record.cost_is_estimate,
    }
    if not record.ok:
        summary = {"error": record.error, "status": record.status}
    elif record.stage == "refine":
        summary = {"refined": True, **summary}
    return StepTrace(
        step_name=step_name(record),
        model_name=record.model_alias,
        provider=record.provider,
        input_tokens=record.input_tokens or 0,
        output_tokens=record.output_tokens or 0,
        latency_ms=record.latency_ms,
        cost_usd=record.cost_usd or 0.0,
        eval_summary=summary,
    )


def _with_shadow_actuals(
    comparison: CostComparison, state: RunState, fusion_usd: float, fusion_known: bool
) -> CostComparison:
    """Replace the baseline *estimate* with the actual cost of the shadow run when we have it."""
    shadow = state.shadow
    if not (
        shadow
        and shadow.ran
        and shadow.baseline_cost_known
        and shadow.baseline_cost_usd is not None
    ):
        return comparison
    savings = shadow.baseline_cost_usd - fusion_usd if fusion_known else None
    return comparison.model_copy(
        update={
            "baseline_estimated_cost_usd": shadow.baseline_cost_usd,
            "baseline_cost_known": True,
            "savings_usd": savings,
            "savings_percent": (
                savings / shadow.baseline_cost_usd * 100
                if savings is not None and shadow.baseline_cost_usd > 0
                else None
            ),
            "fusion_is_cheaper": savings > 0 if savings is not None else None,
            "comparison_notes": [
                *comparison.comparison_notes,
                "Baseline cost and latency are actual values from a shadow run, not estimates.",
            ],
        }
    )


def default_stages(deps: PipelineDeps) -> list[Stage]:
    return [
        RedactStage(deps),
        RouteStage(deps),
        ContextEvalStage(deps),
        PanelStage(deps),
        RefineStage(deps),
        JudgeStage(deps),
        AggregateStage(deps),
        FinalEvalStage(deps),
        ShadowStage(deps),
        PersistStage(deps),
    ]
