"""Pipeline stages. Each reads and writes only ``RunState``; nothing else is shared.

Order: Redact -> Route -> ContextEval -> Budget -> ShadowStart -> Panel -> Refine -> Claims ->
(Judge | Aggregate) -> FinalEval -> Shadow -> Persist. Judge and Aggregate run at the same time
unless the strategy's synthesis needs the judge's scores; the shadow baseline runs alongside the
panel and is collected by Shadow. A stage may halt the run (``state.halt``); every later stage is
then skipped except those marked ``always_runs`` (Persist), which still stores a diagnostic
result.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from fusion.benchmark.shadow import (
    call_shadow_baseline,
    finish_shadow_comparison,
    should_run_shadow,
)
from fusion.config.loader import FanoutConfig
from fusion.orchestration.aggregate import (
    aggregator_for,
    build_best_of,
    build_digest,
    build_patch_vote,
    build_verified,
    build_vote,
)
from fusion.orchestration.budget_guard import (
    escalation_calls,
    judge_calls,
    preflight,
    refine_round_calls,
    synthesis_calls,
)
from fusion.orchestration.cascade import CascadeOutcome, cheapest_first, decide, merge_fanouts
from fusion.orchestration.claims import (
    AgreementReport,
    ClaimCluster,
    PanelAnswer,
    agreement_score,
    cluster_claims,
    parse_panel_answer,
    render_panel_answer,
)
from fusion.orchestration.context import Halt, PipelineDeps, RunState
from fusion.orchestration.disagreement import analyze_disagreement
from fusion.orchestration.fanout import FanoutResult, fanout_to_panel
from fusion.orchestration.judge import judge_panel_responses
from fusion.orchestration.ledger import CallRecord
from fusion.orchestration.output import step_name
from fusion.orchestration.output_parser import parse_structured_output
from fusion.orchestration.progress import report as report_progress
from fusion.orchestration.prompts import build_user_prompt, get_system_prompt
from fusion.orchestration.refine import RefinementResult, refine_panel_responses
from fusion.orchestration.result import PanelResult, PipelineResult, build_usage_summary
from fusion.orchestration.strategy import PanelMember, Strategy
from fusion.orchestration.synthesize import synthesize_responses
from fusion.providers.base import ModelResponse
from fusion.routing.budget import ANSWER_OUTPUT_TOKENS, PlannedCall, estimate_tokens
from fusion.routing.classifier import canonical_task_key
from fusion.security.redaction import redact_secrets
from fusion.storage.run_store import ShadowComparisonRecord
from fusion.telemetry.cost import CostComparison, compare_to_baseline
from fusion.telemetry.traces import OrchestrationTrace, StepTrace


class Stage(Protocol):
    """A pipeline step. Optional class attributes (read with ``getattr``): ``label`` is the
    progress message sent when the stage starts, and ``soft_limited`` puts the stage under the
    soft time limit (see ``SoftTimeoutRecovery``)."""

    always_runs: bool

    async def run(self, state: RunState) -> RunState: ...


class _Stage:
    always_runs = False
    label: str | None = None
    soft_limited = False

    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps


# --------------------------------------------------------------------------------------- redact


class RedactStage(_Stage):
    """Redact secrets from the input and open the run record."""

    async def run(self, state: RunState) -> RunState:
        ctx = state.ctx
        if state.redact:
            primary = redact_secrets(ctx.primary_content)
            context = redact_secrets(ctx.context)
            state.sanitized_primary = primary.text
            state.sanitized_context = context.text
            state.sanitized_snippets = [redact_secrets(s).text for s in ctx.file_snippets]
            state.redaction_count = primary.redaction_count + context.redaction_count
        else:  # benchmark mode: the task reaches the models exactly as written
            state.sanitized_primary = ctx.primary_content
            state.sanitized_context = ctx.context
            state.sanitized_snippets = list(ctx.file_snippets)

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

    label = "routing"

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
        state.disagreement = {
            "disagreement_score": 0.0,
            "agreement_score": 0.0,
            "low_information": True,
            "consensus": True,
            "outlier_models": [],
        }
        state.evals = self.deps.presenter.build_evals(
            state.context_eval, [], {}, state.final_eval, None, state.warnings
        )
        state.panel_models = []
        if state.trace is not None:
            state.trace.panel_models = []
        state.halt = Halt("insufficient_context")
        state.stamp_latency(self.deps.clock)
        return state


# --------------------------------------------------------------------------------------- budget


class BudgetStage(_Stage):
    """Hold the run to its strategy's ``max_cost_usd`` before any model is called.

    The planned calls are priced from assumed token counts. If they would pass the cap the
    strategy is shifted down (see ``budget_guard.down_shifts``) with a warning; if even the
    cheapest shift is over, the run halts having spent nothing. Without a cap this stage only
    measures the prompt. The mid-run half of the cap lives in ``BudgetGuard``.
    """

    async def run(self, state: RunState) -> RunState:
        strategy = state.strategy
        assert strategy is not None
        parts = [state.sanitized_primary, state.sanitized_context, *state.sanitized_snippets]
        state.prompt_tokens = estimate_tokens("\n".join(p for p in parts if p))
        if strategy.max_cost_usd is None:
            return state
        plan = strategy.model_copy(
            update={
                "members": state.members,
                "aggregator_model": state.synthesizer_model or None,
            }
        )
        found = preflight(
            plan,
            models=self.deps.registry.models,
            pricing=self.deps.pricing,
            judge_model=state.judge_model,
            prompt_tokens=state.prompt_tokens,
        )
        state.preflight = found
        cap = strategy.max_cost_usd
        if found.shifts:
            self._shifted(state, found.shifts, found.requested.usd, found.forecast.usd, cap)
        if found.within_cap is None:
            missing = ", ".join(found.forecast.unpriced)
            state.warnings.append(
                f"Cost cap of ${cap:.4f} cannot be checked up front: no price for {missing}. "
                "It is still enforced between stages."
            )
        if found.within_cap is False:
            self._halt(state, found.forecast.usd, cap)
        return state

    def _shifted(
        self, state: RunState, steps: list[str], asked: float, now: float, cap: float
    ) -> None:
        found = state.preflight
        assert found is not None and state.strategy is not None
        message = (
            f"Strategy '{state.strategy.name}' was forecast at ${asked:.4f}, over its ${cap:.4f} "
            f"cap; {', '.join(steps)} (now about ${now:.4f})"
        )
        state.warnings.append(message)
        if found.within_cap is False:
            return  # the run halts, so there is nothing to reconfigure
        shifted = found.strategy
        state.strategy = shifted
        state.members = list(shifted.members)
        state.panel_models = [m.model for m in shifted.members]
        if shifted.kind == "solo" or shifted.aggregator != "llm":
            state.synthesizer_model = ""
        if state.routing is not None:
            state.routing.selected_panel = state.panel_models
            state.routing.synthesizer_model = state.synthesizer_model
            state.routing.reasons.append(f"Cost cap: {', '.join(steps)}")
        if state.trace is not None:
            state.trace.panel_models = state.panel_models

    def _halt(self, state: RunState, forecast: float, cap: float) -> None:
        assert state.strategy is not None and state.context_eval is not None
        engine = self.deps.eval_engine
        name = state.strategy.name
        text = (
            f"Cost cap not met: the cheapest way to run '{name}' is forecast at about "
            f"${forecast:.4f}, over its ${cap:.4f} cap, so no model was called. Raise "
            f"strategies.{name}.max_cost_usd or choose a cheaper strategy."
        )
        state.final_answer = text
        state.final_eval = engine.evaluate_final(
            text, is_coding_task=engine.is_coding_task(state.task_type)
        )
        state.disagreement = {
            "disagreement_score": 0.0,
            "agreement_score": 0.0,
            "low_information": True,
            "consensus": False,
            "outlier_models": [],
        }
        state.structured = {
            "summary": text,
            "budget_exceeded": True,
            "forecast_cost_usd": forecast,
            "max_cost_usd": cap,
        }
        state.evals = self.deps.presenter.build_evals(
            state.context_eval, [], state.disagreement, state.final_eval, None, state.warnings
        )
        state.panel_models = []
        if state.trace is not None:
            state.trace.panel_models = []
        state.halt = Halt("budget")
        state.stamp_latency(self.deps.clock)


# ---------------------------------------------------------------------------------------- panel


class PanelStage(_Stage):
    """Fan the task out to the panel; halt when too few models answered.

    A cascade asks its cheapest members first and only asks the rest when they disagree.
    """

    soft_limited = True

    async def run(self, state: RunState) -> RunState:
        strategy = state.strategy
        assert strategy is not None
        if strategy.kind == "cascade":
            assert strategy.cascade is not None
            if len(state.panel_models) > strategy.cascade.first:
                return await self._cascade(state, strategy)
            state.warnings.append(
                f"Cascade needs more than {strategy.cascade.first} available models to have "
                "anyone to escalate to; ran them as one panel"
            )
        return self._accept(state, await self._fan(state, state.panel_models))

    async def _fan(
        self,
        state: RunState,
        models: list[str],
        *,
        min_successful: int | None = None,
        label: str = "panel",
    ) -> FanoutResult:
        noun = "model" if len(models) == 1 else "models"
        await report_progress(f"{label}: asking {len(models)} {noun}")
        return await fanout_to_panel(
            panel_models=models,
            registry_models=self.deps.registry.models,
            providers=self.deps.providers,
            task_type=state.task_type,
            primary_content=state.sanitized_primary,
            context=state.sanitized_context,
            file_snippets=state.sanitized_snippets,
            changed_files=state.ctx.changed_files,
            config=self._fanout_config(state),
            gateway=state.gateway,
            members={m.model: m for m in state.members},
            min_successful=min_successful,
            label=label,
        )

    def _fanout_config(self, state: RunState) -> FanoutConfig:
        """The routing policy's fan-out settings with the strategy's own latency controls on top."""
        config = self.deps.routing.budgets.fanout
        override = state.strategy.fanout if state.strategy else None
        if override is None:
            return config
        updates = {
            name: value
            for name in override.model_fields_set
            if (value := getattr(override, name)) is not None
        }
        return config.model_copy(update=updates)

    def _accept(self, state: RunState, fanout: FanoutResult) -> RunState:
        state.fanout = fanout
        state.warnings.extend(fanout.warnings)
        state.successful = fanout.successful
        if not fanout.quorum_met:
            self._halt_without_quorum(state)
        return state

    # -- cascade ----------------------------------------------------------------------------

    async def _cascade(self, state: RunState, strategy: Strategy) -> RunState:
        spec = strategy.cascade
        assert spec is not None and state.routing is not None
        ordered = cheapest_first(state.members, self.deps.registry.models, self.deps.pricing)
        state.members = ordered
        state.panel_models = [m.model for m in ordered]
        first_wave = state.panel_models[: spec.first]
        first = await self._fan(state, first_wave, min_successful=2)
        measured = measure_claims(state, successful=first.successful, n_requested=len(first_wave))
        stop, reason = decide(measured.report, risk=state.routing.risk, spec=spec)
        over_budget = None
        if not stop:
            over_budget = state.guard.check("escalation", self._escalation(state, strategy, first))
            if over_budget:
                stop, reason = True, f"{over_budget}; kept the first wave's answer"
                state.warnings.append(f"{over_budget}; the cascade did not escalate")
        outcome = CascadeOutcome(
            exited_early=stop,
            reason=reason,
            agreement=measured.report.score,
            threshold=spec.agreement_threshold,
            risk=state.routing.risk,
            first_wave=first_wave,
            stopped_by_budget=over_budget is not None,
        )
        state.cascade = outcome
        state.routing.reasons.append(f"Cascade: {reason}")
        if stop:
            state.panel_models, state.members = first_wave, ordered[: spec.first]
            state.synthesizer_model = state.routing.synthesizer_model = ""  # none will be called
            if state.trace is not None:
                state.trace.panel_models = first_wave
            return self._accept(state, first)
        rest = state.panel_models[spec.first :]
        outcome.escalated_to = rest
        second = await self._fan(state, rest, label="escalation")
        state.guard.release("escalation")
        merged = merge_fanouts(
            first,
            second,
            order=state.panel_models,
            min_successful=self.deps.routing.budgets.fanout.min_successful_responses,
        )
        return self._accept(state, merged)

    @staticmethod
    def _escalation(state: RunState, strategy: Strategy, first: FanoutResult) -> list[PlannedCall]:
        sized = [r.output_tokens or ANSWER_OUTPUT_TOKENS for _, r in first.successful]
        plan = strategy.model_copy(
            update={
                "members": state.members,
                "aggregator_model": state.synthesizer_model or None,
            }
        )
        return escalation_calls(plan, prompt_tokens=state.prompt_tokens, first_wave=sized)

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
            "agreement_score": 0.0,
            "low_information": True,
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
    Every refinement call of a round starts together once all round-1 answers exist, and a round
    is skipped when the panel already agrees (``refinement.skip_above_agreement``).
    """

    label = "refining the panel's answers"
    soft_limited = True

    async def run(self, state: RunState) -> RunState:
        assert state.strategy is not None
        if state.cascade_exited_early:
            return state  # the cheap first wave agreed; refining is for the escalated panel
        config = self.deps.routing.budgets.refinement
        members = {m.model: m for m in state.members}
        for _ in range(state.strategy.rounds - 1):
            if self._already_agrees(state, config.skip_above_agreement):
                break
            if self._over_cap(state):
                break
            try:
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
            finally:
                state.guard.release("refinement")
            merged = round_result
            if state.refinement is not None:
                merged = state.refinement.merged(round_result)
            state.refinement = merged
            state.warnings.extend(round_result.warnings)
            if not round_result.ran:
                break
        return state

    @staticmethod
    def _over_cap(state: RunState) -> bool:
        """True (with a warning) when the cost or latency cap leaves no room for a round."""
        sizes = [r.output_tokens or ANSWER_OUTPUT_TOKENS for _, r in state.successful]
        calls = refine_round_calls([m for m, _ in state.successful], state.prompt_tokens, sizes)
        reason = state.guard.check("refinement", calls)
        if reason:
            state.warnings.append(f"{reason}; skipped the remaining refinement")
        return reason is not None

    @staticmethod
    def _already_agrees(state: RunState, threshold: float | None) -> bool:
        if threshold is None or len(state.successful) < 2:
            return False
        report = measure_claims(state).report
        if report.score < threshold:
            return False
        message = (
            f"Refinement skipped: panel agreement {report.score:.2f} is at least {threshold:.2f}"
        )
        state.warnings.append(message)
        state.refinement = state.refinement or RefinementResult(warnings=[message])
        return True


# --------------------------------------------------------------------------------------- claims


@dataclass
class Measured:
    """The panel's answers read as claims, clustered, with the agreement between them."""

    answers: dict[str, PanelAnswer]
    structured: dict[str, bool]
    clusters: list[ClaimCluster]
    report: AgreementReport


def measure_claims(
    state: RunState,
    *,
    successful: list[tuple[str, ModelResponse]] | None = None,
    n_requested: int | None = None,
) -> Measured:
    """Parse panel answers (the current ones by default), cluster their claims, score agreement."""
    key = canonical_task_key(state.task_type)
    answers: dict[str, PanelAnswer] = {}
    structured: dict[str, bool] = {}
    for model, response in state.successful if successful is None else successful:
        answers[model], structured[model] = parse_panel_answer(
            response.content, response.parsed_json, task_key=key
        )
    clusters = cluster_claims(answers)
    report = agreement_score(
        clusters,
        len(answers),
        n_requested=len(state.panel_models) if n_requested is None else n_requested,
        n_structured=sum(structured.values()),
    )
    return Measured(answers, structured, clusters, report)


class ClaimsStage(_Stage):
    """Read each answer as claims, group them across models, and measure the agreement."""

    async def run(self, state: RunState) -> RunState:
        measured = measure_claims(state)
        state.answers, state.answer_structured = measured.answers, measured.structured
        state.clusters, state.agreement = measured.clusters, measured.report
        state.disagreement = analyze_disagreement(
            state.clusters, measured.report, known_files=state.ctx.changed_files or None
        )
        state.warnings.extend(_claims_warnings(state))
        return state


def _claims_warnings(state: RunState) -> list[str]:
    report = state.agreement
    assert report is not None
    found: list[str] = []
    prose = [m for m, ok in state.answer_structured.items() if not ok]
    if prose:
        found.append(
            f"{len(prose)} of {len(state.answers)} panel answers were not valid claims JSON "
            f"({', '.join(prose)}); their claims were read from list items"
        )
    if report.low_information:
        found.append(
            f"Confidence is low-information: {report.n_models} panel answer(s), so there was no "
            "agreement to measure (confidence is capped at 0.50)"
        )
    return found


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
        use_llm = judge != "off" and not self._over_cap(state)
        try:
            state.evaluations = await judge_panel_responses(
                eval_engine=engine,
                responses=state.successful,
                task_type=state.task_type.value,
                judge_model=state.judge_model,
                context=state.sanitized_context,
                is_coding_task=engine.is_coding_task(state.task_type),
                known_files=state.ctx.changed_files or None,
                gateway=state.gateway,
                use_llm=use_llm,
                answers=state.answers,
                structured=state.answer_structured,
            )
            if state.evaluations and engine.use_llm_judge and use_llm and judge == "full":
                await self._check_judge_quality(state)
        finally:
            state.guard.release("judge")
        state.panel_results = self._panel_results(state)
        return state

    @staticmethod
    def _over_cap(state: RunState) -> bool:
        """True (with a warning) when the cost or latency cap leaves no room for the judge."""
        assert state.strategy is not None
        sizes = [r.output_tokens or ANSWER_OUTPUT_TOKENS for _, r in state.successful]
        calls = judge_calls(
            state.judge_model, state.prompt_tokens, sizes, full=state.strategy.judge == "full"
        )
        reason = state.guard.check("judge", calls)
        if reason:
            state.warnings.append(f"{reason}; answers were scored by deterministic checks only")
        return reason is not None

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
    """Produce the final answer the way the strategy says (see ``aggregate.aggregator_for``).

    ``solo`` returns the one member's answer. ``digest``, ``vote`` and ``best_of`` build the answer
    from the panel's own claims with no model call: the digest hands everything to Claude Code to
    merge, a vote keeps the points a majority backed, best-of returns the answer the others agree
    with most. ``llm`` makes one synthesizer call that sees the clusters as well as the answers,
    unless the cost or latency cap leaves no room for it, in which case the digest is returned.
    """

    async def run(self, state: RunState) -> RunState:
        assert state.strategy is not None and state.agreement is not None
        readable = _readable_answers(state)
        kind = aggregator_for(state.strategy, cascade_exited_early=state.cascade_exited_early)
        if kind == "solo":
            response = state.successful[0][1]
            state.synth_response = response.model_copy(update={"text": readable[0][1]})
        elif kind == "digest":
            state.synth_response = build_digest(readable, state.clusters, state.agreement)
        elif kind == "vote":
            state.synth_response = self._vote(state, readable)
        elif kind == "best_of":
            state.synth_response = self._best_of(state, readable)
        elif kind == "verified":
            state.synth_response = await self._verified(state, readable)
        else:
            state.synth_response = await self._synthesize(state, readable)
        return state

    @staticmethod
    def _vote(state: RunState, readable: list[tuple[str, str]]) -> ModelResponse:
        assert state.agreement is not None
        if state.ctx.expects_patch:
            voted = build_patch_vote(readable, state.answers, state.clusters)
            if voted is not None:
                response, picked, votes = voted
                if state.routing is not None:
                    state.routing.reasons.append(
                        f"patch vote picked {picked.model}'s patch ({votes} of {len(readable)} "
                        "models gave it)"
                    )
                return response
            state.warnings.append("Vote: no answer contained a patch; returned the best answer")
            return AggregateStage._best_of(state, readable)
        if not state.agreement.consensus:
            if state.cascade is not None and state.cascade.stopped_by_budget:
                state.warnings.append("No point reached a majority; returned the best answer")
                return AggregateStage._best_of(state, readable)
            state.warnings.append(
                "Vote: no point was backed by a majority of the models, so nothing was kept; "
                "use the panel-digest strategy to see every answer"
            )
        return build_vote(state.clusters, state.agreement, len(readable))

    @staticmethod
    def _best_of(state: RunState, readable: list[tuple[str, str]]) -> ModelResponse:
        response, picked = build_best_of(readable, state.clusters)
        if state.routing is not None:
            state.routing.reasons.append(
                f"best_of picked {picked.model} (agreement with the others {picked.agreement:.2f})"
            )
        return response

    @staticmethod
    async def _verified(state: RunState, readable: list[tuple[str, str]]) -> ModelResponse:
        """The panel's patch that does best on the task's visible tests (benchmark mode only)."""
        verify = state.ctx.verifier
        if verify is None:
            state.warnings.append(
                "The verified aggregator needs the benchmark's test runner; none was given, so "
                "the best answer was returned"
            )
            return AggregateStage._best_of(state, readable)
        verified = await build_verified(readable, state.answers, state.clusters, verify)
        if verified is None:
            state.warnings.append("Verified: no answer contained a patch; returned the best answer")
            return AggregateStage._best_of(state, readable)
        response, picked, scores = verified
        if state.routing is not None:
            shown = ", ".join(
                f"{m} {'?' if v is None else f'{v:.2f}'}" for m, v in scores.items()
            )
            state.routing.reasons.append(
                f"verified picked {picked.model}'s patch by visible tests ({shown})"
            )
        return response

    async def _synthesize(self, state: RunState, readable: list[tuple[str, str]]) -> ModelResponse:
        assert state.agreement is not None
        sizes = [r.output_tokens or ANSWER_OUTPUT_TOKENS for _, r in state.successful]
        calls = synthesis_calls(state.synthesizer_model, state.prompt_tokens, sizes)
        reason = state.guard.check("synthesis", calls)
        if reason:
            state.warnings.append(
                f"{reason}; returned the panel digest for Claude Code to merge instead"
            )
            return build_digest(readable, state.clusters, state.agreement)
        try:
            return await synthesize_responses(
                synthesizer_model=state.synthesizer_model,
                registry_models=self.deps.registry.models,
                providers=self.deps.providers,
                task_type=state.task_type,
                panel_responses=readable,
                disagreement_analysis=_agreement_summary(state),
                original_task=state.sanitized_primary,
                gateway=state.gateway,
                clusters=state.clusters,
                patch=state.ctx.expects_patch,
            )
        finally:
            state.guard.release("synthesis")


def _readable_answers(state: RunState) -> list[tuple[str, str]]:
    """Each panelist's answer as text: claims rendered as Markdown, prose left as it came."""
    return [
        (m, render_panel_answer(state.answers[m]) if state.answer_structured[m] else r.content)
        for m, r in state.successful
    ]


def _agreement_summary(state: RunState) -> dict[str, object]:
    keys = ("agreement_score", "low_information", "outlier_models", "contradictions")
    summary: dict[str, object] = {
        k: state.disagreement[k] for k in keys if k in state.disagreement
    }
    assert state.strategy is not None
    if state.strategy.judge_feeds_synthesis and state.evaluations:
        summary["judge_scores"] = {
            e.model_name: round(e.overall_score, 2) for e in state.evaluations
        }
    return summary


# ----------------------------------------------------------------------------------- final eval


class FinalEvalStage(_Stage):
    """Evaluate and parse the final answer, then settle budget warnings and wall time."""

    label = "checking the final answer"

    async def run(self, state: RunState) -> RunState:
        engine = self.deps.eval_engine
        synth = state.synth_response
        assert synth is not None and state.context_eval is not None  # set by earlier stages
        state.final_answer = synth.content
        state.final_eval = engine.evaluate_final(
            synth.content,
            is_coding_task=engine.is_coding_task(state.task_type),
            known_files=state.ctx.changed_files or None,
            clusters=state.clusters,
            report=state.agreement,
        )
        state.structured = parse_structured_output(
            state.task_type,
            synth.content,
            disagreement=state.disagreement,
            confidence=state.final_eval.confidence,
            clusters=state.clusters,
            summary=_headline(state),
            score=_mean_score(state),
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


def _headline(state: RunState) -> str:
    """Summary for fields built from claims: the panelists' own summaries."""
    summaries = [(m, a.summary.strip()) for m, a in state.answers.items() if a.summary.strip()]
    if len(summaries) == 1:
        return summaries[0][1]
    return "\n".join(f"{m}: {s}" for m, s in summaries)


def _mean_score(state: RunState) -> float | None:
    scores = [a.score for a in state.answers.values() if a.score is not None]
    return sum(scores) / len(scores) if scores else None


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


def _shadow_prompt(state: RunState) -> str:
    ctx = state.ctx
    return build_user_prompt(
        task_type=state.task_type,
        primary_content=state.sanitized_primary,
        context=state.sanitized_context,
        file_snippets=state.sanitized_snippets,
        changed_files=ctx.changed_files,
        claims=False,
    )


class ShadowStartStage(_Stage):
    """Start the shadow baseline call now, alongside the panel (it needs only the task prompt)."""

    async def run(self, state: RunState) -> RunState:
        if not state.mode_settings.shadow or not should_run_shadow(state.ctx.shadow_baseline):
            return state
        state.shadow_task = asyncio.create_task(
            call_shadow_baseline(
                task_prompt=_shadow_prompt(state),
                system_prompt=get_system_prompt(state.task_type),
                providers=self.deps.providers,
                baseline=self.deps.shadow_baseline,
                gateway=state.gateway,
            )
        )
        return state


class ShadowStage(_Stage):
    """Collect the baseline's answer and judge it blind against Fusion's (measurement only).

    Shadow calls are never counted as Fusion cost, and Fusion's wall time was stamped before
    waiting for them.
    """

    async def run(self, state: RunState) -> RunState:
        task = state.shadow_task
        if task is None:
            return state
        assert state.synth_response is not None
        baseline_call = await task
        fusion_cost = state.ledger.total_cost()
        shadow = await finish_shadow_comparison(
            baseline_call=baseline_call,
            task_prompt=_shadow_prompt(state),
            fusion_answer=state.synth_response.content,
            fusion_cost_usd=fusion_cost.usd if fusion_cost.known else None,
            fusion_latency_ms=state.total_latency_ms,
            registry_models=self.deps.registry.models,
            providers=self.deps.providers,
            judge_model_alias=state.judge_model,
            pricing=self.deps.pricing,
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
            claims=state.clusters,
            agreement=state.agreement,
            cascade=state.cascade,
            budget=state.budget_report(),
            halt_reason=state.halt.reason if state.halt else None,
            partial=state.partial,
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


class SoftTimeoutRecovery(_Stage):
    """Turn a run the soft time limit interrupted into the best answer it can still give.

    Nothing here calls a model. When the panel had answered, the missing steps are finished from
    those answers (claims, deterministic scores, and the digest as the final answer, which leaves
    the merging to Claude Code); otherwise the run halts with an explanation. Either way the
    result says why it is incomplete.
    """

    async def run(self, state: RunState) -> RunState:
        strategy = state.strategy
        assert strategy is not None
        limit = state.soft_limit_s
        state.partial = True
        if state.fanout is None or not state.successful:
            self._halt(state, limit)
            return state
        state.warnings.append(
            f"Soft time limit of {limit:.0f}s reached; returning the panel's digest instead of "
            "waiting for the remaining steps (FUSION_TOOL_SOFT_TIMEOUT_S)"
        )
        if state.agreement is None:
            state = await ClaimsStage(self.deps).run(state)
        if not state.panel_results:
            state.strategy = strategy = strategy.model_copy(update={"judge": "off"})
            state = await JudgeStage(self.deps).run(state)
        if state.synth_response is None:
            state.strategy = strategy.model_copy(
                update={"aggregator": "digest", "aggregator_model": None}
            )
            state.synthesizer_model = ""
            state = await AggregateStage(self.deps).run(state)
        return await FinalEvalStage(self.deps).run(state)

    def _halt(self, state: RunState, limit: float) -> None:
        assert state.context_eval is not None
        engine = self.deps.eval_engine
        text = (
            f"No answer was ready after {limit:.0f}s (the soft time limit), so the run was "
            "stopped. Try a cheaper or faster strategy (for example solo-cheap), a smaller "
            "input, or raise FUSION_TOOL_SOFT_TIMEOUT_S."
        )
        state.final_answer = text
        state.final_eval = engine.evaluate_final(
            text, is_coding_task=engine.is_coding_task(state.task_type)
        )
        state.disagreement = {
            "disagreement_score": 0.0,
            "agreement_score": 0.0,
            "low_information": True,
            "consensus": False,
            "outlier_models": [],
        }
        state.structured = {"summary": text, "timed_out": True, "soft_limit_s": limit}
        state.evals = self.deps.presenter.build_evals(
            state.context_eval, [], state.disagreement, state.final_eval, None, state.warnings
        )
        state.halt = Halt("timeout")
        state.stamp_latency(self.deps.clock)


class ConcurrentStages:
    """Run independent stages at the same time over the one ``RunState``.

    The stages must write different parts of the state. ``sequential_if`` names the case where
    one needs another's output, and they then run in order. If one fails, the others are
    cancelled and its exception is raised as it was.
    """

    always_runs = False
    label: str | None = "synthesizing"
    soft_limited = True

    def __init__(
        self, *stages: Stage, sequential_if: Callable[[RunState], bool] = lambda _state: False
    ) -> None:
        self.stages = stages
        self._sequential_if = sequential_if

    async def run(self, state: RunState) -> RunState:
        if state.halted:
            return state
        if self._sequential_if(state):
            for stage in self.stages:
                state = await stage.run(state)
            return state
        tasks = [asyncio.ensure_future(stage.run(state)) for stage in self.stages]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return state


def _judge_feeds_synthesis(state: RunState) -> bool:
    return state.strategy is not None and state.strategy.judge_feeds_synthesis


def default_stages(deps: PipelineDeps) -> list[Stage]:
    return [
        RedactStage(deps),
        RouteStage(deps),
        ContextEvalStage(deps),
        BudgetStage(deps),
        ShadowStartStage(deps),
        PanelStage(deps),
        RefineStage(deps),
        ClaimsStage(deps),
        ConcurrentStages(
            JudgeStage(deps), AggregateStage(deps), sequential_if=_judge_feeds_synthesis
        ),
        FinalEvalStage(deps),
        ShadowStage(deps),
        PersistStage(deps),
    ]
