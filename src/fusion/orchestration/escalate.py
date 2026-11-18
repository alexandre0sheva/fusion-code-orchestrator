"""``strategy: auto``: start cheap, and spend more only when the cheap answer shows it must.

Most requests do not need a panel. An auto run estimates the request's difficulty
(``fusion.routing.difficulty``: deterministic signals, plus one nano-model call when they are
ambiguous) and starts on the matching rung of the ladder in ``routing.escalation``: by default
one nano model with a self-check (easy), the nano panel with lenses or approaches (normal), the
mid panel (hard), and frontier solo, which is off unless ``allow_frontier``. Each rung is an
ordinary strategy run by the ordinary stages.

After a rung, the answer is checked for the triggers that say it is not enough (any one):

====================  =======================================================================
``low_confidence``    a single model's self-reported confidence is below the threshold
``low_agreement``     a panel whose members answer the same question the same way (no lenses,
                      no approaches) agrees less than tau
``none_acceptable``   the ``select`` aggregator marked no candidate acceptable
``no_backed_finding`` ``union`` kept no finding the input backs where findings were expected
``patches_fail``      every candidate patch failed the checks (the ``verified`` aggregator)
``no_quorum``         a model error left no quorum
====================  =======================================================================

The first three are known before aggregation, so a rung that escalates on them is never merged
(no synthesis or selection call is paid for an answer that will not be returned). A higher rung
**reuses** everything below it: models that already answered are not asked again, and their
answers join the new ones. Before each climb the cost cap and the soft time limit are checked;
when the next rung would pass either (or ``max_rungs`` is reached, or the next rung is a frontier
rung that is not allowed) the run returns the best answer so far and says so.

``DifficultyStage`` (after redaction) picks the start rung and swaps the run's strategy for the
rung's; ``EscalateStage`` (after the claims stage) checks, aggregates and climbs. Both do nothing
for any strategy but ``kind: auto``.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from fusion.config.catalog import speed_rank
from fusion.config.loader import DifficultyConfig, EscalationPolicy, EscalationTriggers, Rung
from fusion.orchestration.aggregate import resolve_aggregator
from fusion.orchestration.budget_guard import plan_calls
from fusion.orchestration.context import Carried, PipelineDeps, RunState
from fusion.orchestration.ledger import SHADOW_STAGES, CallRecord
from fusion.orchestration.lenses import base_alias
from fusion.orchestration.progress import report as report_progress
from fusion.orchestration.strategy import (
    SELECT_AGGREGATORS,
    PanelMember,
    Strategy,
    may_run_code,
)
from fusion.providers.base import ModelRequest
from fusion.routing.budget import ANSWER_OUTPUT_TOKENS, estimate_tokens, forecast_calls
from fusion.routing.difficulty import (
    DIFFICULTY_SYSTEM_PROMPT,
    DifficultyEstimate,
    RequestFacts,
    build_difficulty_prompt,
    difficulty_schema,
    estimate_difficulty,
    parse_difficulty_reply,
)

__all__ = [
    "Climb",
    "DifficultyStage",
    "EscalateStage",
    "EscalationOutcome",
    "EscalationPolicy",
    "Rung",
    "RungAttempt",
    "Verdict",
    "climbable_rungs",
    "early_verdict",
    "late_verdict",
]

Trigger = Literal[
    "low_confidence",
    "low_agreement",
    "none_acceptable",
    "no_backed_finding",
    "patches_fail",
    "no_quorum",
]


# ------------------------------------------------------------------------------- the run record


class RungAttempt(BaseModel):
    """One rung a run tried: who it asked, whose answers it reused, what it cost, and why it
    answered, escalated or stopped."""

    rung: int
    name: str
    strategy: str
    asked: list[str] = Field(default_factory=list)
    reused: list[str] = Field(default_factory=list)
    # answered: its answer was returned; escalated: a trigger fired and the next rung ran;
    # stopped: a trigger fired but no higher rung could run; skipped: every member had already
    # answered below; interrupted: the soft time limit cut it short.
    outcome: Literal["answered", "escalated", "stopped", "skipped", "interrupted"] = "interrupted"
    trigger: Trigger | None = None
    reason: str = ""
    cost_usd: float = 0.0
    cost_known: bool = True
    latency_ms: float = 0.0


class EscalationOutcome(BaseModel):
    """What an auto run did: the difficulty estimate, every rung tried and which one answered."""

    difficulty: DifficultyEstimate
    classifier_cost_usd: float | None = None  # the classifier call, when the signals were unclear
    start_rung: int
    start_reason: str = ""
    rungs: list[RungAttempt] = Field(default_factory=list)
    answered_at: int | None = None
    stopped: str | None = None  # why the climb ended below where a trigger asked it to go
    summary: str = ""  # "answered at rung 1 (nano-panel): panel agreed (0.86)"


def climbable_rungs(policy: EscalationPolicy) -> list[int]:
    """Indexes of the rungs a run may use: every rung but a frontier one without allow_frontier."""
    return policy.climbable()


@dataclass
class Climb:
    """The ladder one auto run is climbing (``RunState.climb``)."""

    policy: EscalationPolicy
    auto: Strategy  # the auto strategy itself: its caps hold for every rung
    outcome: EscalationOutcome
    index: int = 0  # the rung now running
    mark: int = 0  # ledger records before the current rung started
    started: float = 0.0  # clock when the current rung started
    notes: list[str] = field(default_factory=list)  # routing reasons of the rungs before

    @property
    def rung(self) -> Rung:
        return self.policy.rungs[self.index]

    @property
    def attempt(self) -> RungAttempt:
        return self.outcome.rungs[-1]

    def begin(self, index: int, strategy: Strategy, ledger_size: int, now: float) -> RungAttempt:
        self.index, self.mark, self.started = index, ledger_size, now
        rung = self.policy.rungs[index]
        attempt = RungAttempt(rung=index, name=rung.name, strategy=strategy.name)
        self.outcome.rungs.append(attempt)
        return attempt


# -------------------------------------------------------------------------------- the triggers


@dataclass(frozen=True)
class Verdict:
    """Whether an answer is enough: ``trigger`` names why not (None: it is), ``reason`` says it."""

    trigger: Trigger | None
    reason: str


def early_verdict(state: RunState, triggers: EscalationTriggers) -> Verdict:
    """The checks known before aggregation: a lost quorum, a single model's own confidence, and
    the agreement of a panel whose members answer the same question the same way."""
    if state.halt is not None and state.halt.reason == "quorum":
        fanout = state.fanout
        got = f"{fanout.success_count} of {len(fanout.calls)}" if fanout else "none"
        return Verdict("no_quorum", f"a model error left no quorum ({got} answered)")
    strategy = state.strategy
    assert strategy is not None
    if strategy.kind == "solo":
        model = state.successful[0][0] if state.successful else ""
        answer = state.answers.get(model)
        if answer is None or not state.answer_structured.get(model):
            return Verdict("low_confidence", "the answer reported no self-check confidence")
        threshold = triggers.self_confidence_below
        if answer.confidence < threshold:
            return Verdict(
                "low_confidence",
                f"self-check confidence {answer.confidence:.2f} is below {threshold:.2f}",
            )
        return Verdict(None, f"self-check confidence {answer.confidence:.2f}")
    report = state.agreement
    plain = not state.lensed and not state.answer_approaches
    merging = strategy.aggregator not in {"union", "union+check", *SELECT_AGGREGATORS}
    if report is not None and plain and merging:
        tau = triggers.agreement_below
        if report.score < tau:
            return Verdict(
                "low_agreement", f"panel agreement {report.score:.2f} is below {tau:.2f}"
            )
        return Verdict(None, f"panel agreed ({report.score:.2f})")
    return Verdict(None, "")


def _expects_findings(state: RunState) -> bool:
    """A debugging request always has a cause to find; otherwise findings are expected when some
    member reported one (members that found nothing in their area say so with none_found)."""
    if state.task_type.value == "debugging":
        return True
    return any(a.claims and not a.none_found for a in state.answers.values())


def late_verdict(state: RunState, early: Verdict) -> Verdict:
    """The checks that need the aggregated answer: the selector's verdict, the union's evidence,
    and the verified patches' test results."""
    selection = state.disagreement.get("selection")
    if isinstance(selection, dict) and selection.get("acceptable") is False:
        why = str(selection.get("why") or "no reason given").splitlines()[0]
        return Verdict("none_acceptable", f"the selector found no candidate acceptable: {why}")
    union = state.union
    if union is not None:
        if not union.backed and _expects_findings(state):
            return Verdict(
                "no_backed_finding",
                f"union kept no finding the input backs ({len(union.unchecked)} unchecked, "
                f"{len(union.dropped)} dropped)",
            )
        kept = len(union.backed)
        found = f"union kept {kept} finding(s) the input backs" if kept else "nothing to report"
        return Verdict(None, found)
    scores = state.disagreement.get("verified")
    if isinstance(scores, dict) and scores:
        passed = [v for v in scores.values() if isinstance(v, int | float)]
        if not passed or max(passed) < 1.0:
            return Verdict("patches_fail", "no candidate patch passed every check")
        return Verdict(None, "a candidate patch passed every check")
    if isinstance(selection, dict):
        if "fallback" in selection:
            return Verdict(
                None, f"the selector gave no usable choice; chose by {selection['fallback']}"
            )
        return Verdict(None, early.reason or "the selector picked an acceptable answer")
    return Verdict(None, early.reason or "the panel answered")


# ------------------------------------------------------------------------- shared helpers


def _rung_strategy(state: RunState, deps: PipelineDeps, climb: Climb, index: int) -> Strategy:
    """The rung's strategy, settled for this task, under the auto run's caps."""
    base = deps.routing.strategies.get(climb.policy.rungs[index].strategy)
    strategy = resolve_aggregator(
        base,
        state.task_type,
        patch=state.ctx.expects_patch,
        verified=state.ctx.crosscheck is not None,
    )
    caps = {}
    for name in ("max_cost_usd", "max_latency_s"):
        values = [v for v in (getattr(climb.auto, name), getattr(strategy, name)) if v is not None]
        caps[name] = min(values) if values else None
    return strategy.model_copy(update=caps)


def _usable(strategy: Strategy, state: RunState) -> bool:
    """A verified aggregator runs code, so a rung using one is skipped where none may run."""
    return may_run_code(strategy, state.mode, crosscheck=state.ctx.crosscheck is not None)


def _records_since(state: RunState, mark: int) -> list[CallRecord]:
    return [r for r in state.ledger.records[mark:] if r.stage not in SHADOW_STAGES]


def _forecast_usd(deps: PipelineDeps, strategy: Strategy, state: RunState) -> float | None:
    """What one run of ``strategy`` is forecast to cost (None when a price is missing)."""
    decision = deps.routing.router.route(
        strategy=strategy, explicit_type=state.task_type.value, content=state.sanitized_primary
    )
    plan = strategy.model_copy(update={"aggregator_model": decision.synthesizer_model or None})
    calls = plan_calls(plan, judge_model=decision.judge_model, prompt_tokens=state.prompt_tokens)
    found = forecast_calls(calls, deps.registry.models, deps.pricing)
    return found.usd if found.known else None


# ------------------------------------------------------------------------------- the start


class DifficultyStage:
    """Estimate the request's difficulty and start the run on the matching rung (auto only)."""

    always_runs = False
    label: str | None = None
    soft_limited = False

    def __init__(self, deps: PipelineDeps) -> None:
        self.deps = deps

    async def run(self, state: RunState) -> RunState:
        auto = state.strategy
        if auto is None or auto.kind != "auto":
            return state
        await report_progress("estimating difficulty")
        routing = self.deps.routing
        policy = routing.escalation_for(auto)
        config = routing.budgets.routing.difficulty
        parts = [state.sanitized_primary, state.sanitized_context, *state.sanitized_snippets]
        state.prompt_tokens = estimate_tokens("\n".join(p for p in parts if p))
        facts = RequestFacts(
            task_type=state.task_type,
            context=state.sanitized_context,
            snippets=tuple(state.sanitized_snippets),
            changed_files=tuple(state.ctx.changed_files),
        )
        estimate = estimate_difficulty(state.sanitized_primary, facts, config)
        cost: float | None = None
        if estimate.ambiguous and config.classifier_model:
            estimate, cost = await self._classify(state, estimate, config)
        outcome = EscalationOutcome(difficulty=estimate, classifier_cost_usd=cost, start_rung=0)
        climb = Climb(policy=policy, auto=auto, outcome=outcome)
        start, why = self._start(state, climb, estimate)
        outcome.start_rung, outcome.start_reason = start, why
        strategy = _rung_strategy(state, self.deps, climb, start)
        climb.begin(start, strategy, len(state.ledger.records), self.deps.clock())
        climb.notes += [f"Difficulty: {estimate.describe()}", f"Ladder: {why}"]
        state.climb = climb
        _apply_caps(state, strategy)
        state.strategy = strategy
        return state

    def _start(
        self, state: RunState, climb: Climb, estimate: DifficultyEstimate
    ) -> tuple[int, str]:
        """The rung the estimate asks for, moved down past a frontier rung that is not allowed,
        past a rung that cannot run here, and past a rung forecast over the cost cap."""
        policy = climb.policy
        wanted = policy.start_by_level[estimate.level]
        allowed = [
            i
            for i in policy.climbable()
            if _usable(_rung_strategy(state, self.deps, climb, i), state)
        ] or [policy.start_for(estimate.level)]
        below = [i for i in allowed if i <= wanted] or [allowed[0]]
        start = max(below)
        name = policy.rungs[wanted].name
        notes = [f"{estimate.level} starts at rung {wanted} ({name})"]
        if start != wanted:
            if policy.rungs[wanted].frontier and not policy.allow_frontier:
                notes.append("a frontier rung needs allow_frontier")
            else:
                notes.append("that rung cannot run here")
        cap = climb.auto.max_cost_usd
        if cap is not None:
            for index in sorted((i for i in below if i <= start), reverse=True):
                usd = _forecast_usd(
                    self.deps, _rung_strategy(state, self.deps, climb, index), state
                )
                if usd is None or usd <= cap:
                    if index != start:
                        notes.append(f"rung {start} is forecast over the ${cap:.4f} cost cap")
                    start = index
                    break
        if start != wanted:
            notes.append(f"started at rung {start} ({policy.rungs[start].name})")
        return start, "; ".join(notes)

    async def _classify(
        self, state: RunState, estimate: DifficultyEstimate, config: DifficultyConfig
    ) -> tuple[DifficultyEstimate, float | None]:
        """One nano-model call settles an ambiguous estimate; its cost is in the run's ledger."""
        model = self._classifier(config.classifier_model or "")
        if model is None:
            note = f"no classifier model is available ({config.classifier_model}); kept the signals"
            return estimate.model_copy(update={"note": note}), None
        entry = self.deps.registry.models[model]
        request = ModelRequest(
            model_id=entry.model_id,
            system_prompt=DIFFICULTY_SYSTEM_PROMPT,
            user_prompt=build_difficulty_prompt(state.sanitized_primary, state.task_type),
            max_tokens=min(entry.max_tokens, config.classifier_max_tokens),
            response_schema=difficulty_schema(),
            response_schema_name="difficulty",
            metadata={
                "task_type": state.task_type.value,
                "role": "difficulty",
                "personality": "judge",
            },
        )
        mark = len(state.ledger.records)
        response = await state.gateway.call(stage="route", alias=model, request=request)
        spent = _records_since(state, mark)
        cost = sum(r.cost_usd or 0.0 for r in spent)
        level = None if response.error else parse_difficulty_reply(response.content)
        if level is None:
            why = response.error or "no level in its reply"
            note = f"the classifier ({model}) could not decide ({why}); kept the signals"
            state.warnings.append(f"Difficulty: {note}")
            return estimate.model_copy(update={"note": note}), cost
        updates = {"level": level, "source": "classifier", "note": f"{model}: {level}"}
        return estimate.model_copy(update=updates), cost

    def _classifier(self, wanted: str) -> str | None:
        registry, providers = self.deps.registry, self.deps.providers
        for alias in (wanted, *registry.by_role("judge")):
            if alias in registry.models and registry.get(alias).provider in providers:
                return alias
        return None


def _apply_caps(state: RunState, strategy: Strategy) -> None:
    """The guard holds the run to the lower of the auto strategy's cap and the rung's own."""
    state.guard.max_cost_usd = strategy.max_cost_usd
    state.guard.max_latency_s = strategy.max_latency_s


# ------------------------------------------------------------------------------- the climb


class _Runs(Protocol):
    async def run(self, state: RunState) -> RunState: ...


@dataclass
class _Next:
    index: int
    state: RunState
    fresh: list[str]


def _fresh_rung(current: RunState, strategy: Strategy) -> RunState:
    """A copy of ``current`` for the next rung: the run's inputs, ledger and guard are kept, and
    everything a rung produces is reset (the answers it reuses are handed over separately, in
    ``RunState.carried``)."""
    return dataclasses.replace(
        current,
        strategy=strategy,
        routing=None,
        members=[],
        panel_models=[],
        lenses={},
        answer_lenses={},
        approaches={},
        answer_approaches={},
        judge_model="",
        synthesizer_model="",
        preflight=None,
        cascade=None,
        fanout=None,
        successful=[],
        refinement=None,
        answers={},
        answer_structured={},
        clusters=[],
        agreement=None,
        evaluations=[],
        judge_quality=None,
        panel_results=[],
        disagreement={},
        synth_response=None,
        union=None,
        chosen="",
        final_answer="",
        structured={},
        final_eval=None,
        evals=None,
        halt=None,
        carried=None,
    )


class EscalateStage:
    """Check each rung's answer, aggregate it, and climb when a trigger fires (auto only).

    It runs after the claims stage, so a rung that escalates on a trigger known before
    aggregation is never merged. It also runs after a lost quorum (``always_runs``): for an auto
    run that is a trigger, not the end. The stages it is given run each higher rung: ``route``,
    ``panel``, ``refine``, ``claims`` and ``aggregate`` (judge and aggregator).
    """

    always_runs = True
    label: str | None = None
    soft_limited = True

    def __init__(
        self,
        deps: PipelineDeps,
        *,
        route: _Runs,
        panel: _Runs,
        refine: _Runs,
        claims: _Runs,
        aggregate: _Runs,
    ) -> None:
        self.deps = deps
        self.route, self.panel, self.refine = route, panel, refine
        self.claims, self.aggregate = claims, aggregate

    async def run(self, state: RunState) -> RunState:
        climb = state.climb
        if climb is None or state.partial:
            return state
        if state.halt is not None and state.halt.reason != "quorum":
            self._close(
                state, climb, "stopped", Verdict(None, f"the run halted: {state.halt.reason}")
            )
            climb.outcome.summary = f"no answer: the run halted ({state.halt.reason})"
            return self._finish(state, climb)
        current = state
        best: tuple[RunState, RungAttempt] | None = None
        triggers = climb.policy.triggers
        while True:
            verdict = early_verdict(current, triggers)
            if verdict.trigger is None:
                current = await self._aggregated(current)
                verdict = late_verdict(current, verdict)
            if verdict.trigger is None:
                self._close(current, climb, "answered", verdict)
                return self._answered(current, climb, climb.attempt, verdict.reason)
            attempt = climb.attempt
            if not current.halted:
                best = (current, attempt)
            step = await self._climb(current, climb)
            if isinstance(step, str):
                self._close(current, climb, "stopped", verdict, attempt=attempt)
                return await self._best_so_far(current, climb, best, step, verdict)
            self._close(current, climb, "escalated", verdict, attempt=attempt)
            current = await self._run(step, climb)

    # -- climbing ------------------------------------------------------------------------------

    async def _climb(self, current: RunState, climb: Climb) -> _Next | str:
        """The next rung to run, prepared; or why the climb stops here."""
        policy = climb.policy
        allowed = climbable_rungs(policy)
        index = climb.index
        while True:
            nxt = index + 1
            if nxt >= len(policy.rungs):
                return f"rung {climb.index} is the top of the ladder"
            if nxt not in allowed:
                rung = policy.rungs[nxt]
                return (
                    f"the next rung, {nxt} ({rung.name}), is a frontier rung and allow_frontier is "
                    "off (use strategy auto-frontier, or set routing.escalation.allow_frontier)"
                )
            tried = sum(1 for a in climb.outcome.rungs if a.outcome != "skipped")
            if tried >= policy.max_rungs:
                return f"max_rungs ({policy.max_rungs}) reached"
            strategy = _rung_strategy(current, self.deps, climb, nxt)
            prepared = await self._prepare(current, climb, nxt, strategy)
            if isinstance(prepared, _Next):
                return prepared
            if prepared:  # a cap or the time limit rules the rung out
                return prepared
            index = nxt  # skipped: nothing new to ask there; look one rung higher

    async def _prepare(
        self, current: RunState, climb: Climb, index: int, strategy: Strategy
    ) -> _Next | str:
        """Route the rung and check it fits; '' when it has nothing new to ask (skipped)."""
        rung = climb.policy.rungs[index]
        if not _usable(strategy, current):
            why = f"its {strategy.aggregator} aggregator runs code"
            self._skip(current, climb, index, strategy, why)
            return ""
        ran = self._phase_seconds(current, climb)  # measured before routing the next rung
        nxt = await self.route.run(_fresh_rung(current, strategy))
        answered = {base_alias(model) for model, _ in current.successful}
        fresh = [m for m in nxt.members if base_alias(m.model) not in answered]
        if not fresh:
            self._skip(current, climb, index, strategy, "every member already answered below")
            return ""
        nxt.members, nxt.panel_models = fresh, [m.model for m in fresh]
        label = f"rung {index} ({rung.name})"
        # time first: a passing cost check holds the rung's money until the rung has run
        reason = self._over_time(nxt, label, ran) or self._over_cap(nxt, label)
        if reason:
            return reason
        if strategy.kind == "panel" and current.successful:
            nxt.carried = _carried(current)
        return _Next(index, nxt, [m.model for m in fresh])

    def _over_cap(self, nxt: RunState, label: str) -> str | None:
        strategy = nxt.strategy
        assert strategy is not None
        _apply_caps(nxt, strategy)
        plan = strategy.model_copy(
            update={"members": nxt.members, "aggregator_model": nxt.synthesizer_model or None}
        )
        calls = plan_calls(plan, judge_model=nxt.judge_model, prompt_tokens=nxt.prompt_tokens)
        return nxt.guard.check(label, calls)

    def _over_time(self, nxt: RunState, label: str, phase_s: float) -> str | None:
        limit = nxt.soft_limit_s
        if not limit:
            return None
        needed = self._rung_seconds(nxt, phase_s)
        elapsed = self.deps.clock() - nxt.started
        if elapsed + needed <= limit:
            return None
        return (
            f"Soft time limit: {label} would take about {needed:.1f}s with {elapsed:.1f}s of the "
            f"{limit:.0f}s limit used"
        )

    @staticmethod
    def _phases(strategy: Strategy) -> int:
        """Sequential steps of a rung: the panel, each refinement round, a model aggregator."""
        return strategy.rounds + (1 if strategy.calls_aggregator_model else 0)

    def _phase_seconds(self, current: RunState, climb: Climb) -> float:
        """How long one step of the rung that just ran took, by the wall clock."""
        assert current.strategy is not None
        ran = self.deps.clock() - climb.started
        done = self._phases(current.strategy) if current.synth_response is not None else 1
        return ran / done

    def _rung_seconds(self, nxt: RunState, phase_s: float) -> float:
        """The rung's expected time: its sequential steps, each as long as the slowest call so
        far or a step of the rung below (or, before any call, the slowest member's measured
        speed)."""
        strategy = nxt.strategy
        assert strategy is not None
        phase = max(nxt.guard.slowest_call_s(), phase_s)
        if phase <= 0:
            models = self.deps.registry.models
            speeds = [speed_rank(models[m], ANSWER_OUTPUT_TOKENS) for m in nxt.panel_models]
            known = [s for s in speeds if math.isfinite(s)]
            phase = max(known, default=0.0) / 1000
        return self._phases(strategy) * phase

    async def _run(self, step: _Next, climb: Climb) -> RunState:
        """Ask the rung's new members, then refine and read claims as any run does."""
        state = step.state
        strategy = state.strategy
        assert strategy is not None
        attempt = climb.begin(step.index, strategy, len(state.ledger.records), self.deps.clock())
        attempt.asked = step.fresh
        attempt.reused = state.carried.models if state.carried else []
        rung = climb.policy.rungs[step.index]
        await report_progress(f"escalating to rung {step.index} ({rung.name})")
        try:
            state = await self.panel.run(state)
            if not state.halted:
                state = await self.refine.run(state)
                state = await self.claims.run(state)
        finally:
            state.guard.release(f"rung {step.index} ({rung.name})")
        state.climb = climb
        return state

    def _skip(
        self, current: RunState, climb: Climb, index: int, strategy: Strategy, why: str
    ) -> None:
        rung = climb.policy.rungs[index]
        climb.outcome.rungs.append(
            RungAttempt(
                rung=index, name=rung.name, strategy=strategy.name, outcome="skipped", reason=why
            )
        )
        climb.notes.append(f"Rung {index} ({rung.name}) skipped: {why}")
        climb.index = index

    # -- closing -------------------------------------------------------------------------------

    async def _aggregated(self, state: RunState) -> RunState:
        if state.synth_response is not None:
            return state
        await report_progress("synthesizing")
        return await self.aggregate.run(state)

    def _close(
        self,
        state: RunState,
        climb: Climb,
        outcome: Literal["answered", "escalated", "stopped"],
        verdict: Verdict,
        *,
        attempt: RungAttempt | None = None,
    ) -> None:
        """Settle a rung's record: outcome, trigger, and the cost and time of its own calls."""
        attempt = attempt or climb.attempt
        if attempt.outcome == "interrupted":  # cost and time are measured once, when it ends
            records = _records_since(state, climb.mark)
            attempt.cost_usd = sum(r.cost_usd or 0.0 for r in records)
            attempt.cost_known = all(r.cost_known for r in records)
            attempt.latency_ms = (self.deps.clock() - climb.started) * 1000
            if not attempt.asked:
                attempt.asked = list(
                    dict.fromkeys(r.model_alias for r in records if r.stage == "panel")
                )
        attempt.outcome = outcome
        attempt.trigger = verdict.trigger
        attempt.reason = verdict.reason
        verb = {"answered": "answered", "escalated": "escalated", "stopped": "not enough"}[outcome]
        line = f"Rung {attempt.rung} ({attempt.name}, {attempt.strategy}) {verb}"
        climb.notes.append(f"{line}: {verdict.reason}" if verdict.reason else line)

    async def _best_so_far(
        self,
        current: RunState,
        climb: Climb,
        best: tuple[RunState, RungAttempt] | None,
        stop: str,
        verdict: Verdict,
    ) -> RunState:
        """No higher rung can run: return the latest rung that produced answers, merged."""
        climb.outcome.stopped = stop
        if best is None:  # nothing ever answered: the halt stands
            current.warnings.append(f"No rung produced an answer; {stop}")
            climb.outcome.summary = f"no answer: {verdict.reason}; {stop}"
            return self._finish(current, climb)
        final, attempt = best
        mark = len(final.ledger.records)
        final = await self._aggregated(final)
        late = _records_since(final, mark)
        attempt.cost_usd += sum(r.cost_usd or 0.0 for r in late)
        final.warnings.append(
            f"Returned the best answer so far (rung {attempt.rung}, {attempt.name}): "
            f"{attempt.reason}; {stop}"
        )
        return self._answered(final, climb, attempt, f"best answer so far; {stop}")

    def _answered(self, final: RunState, climb: Climb, attempt: RungAttempt, why: str) -> RunState:
        climb.outcome.answered_at = attempt.rung
        summary = f"answered at rung {attempt.rung} ({attempt.name})"
        climb.outcome.summary = f"{summary}: {why}" if why else summary
        return self._finish(final, climb)

    def _finish(self, final: RunState, climb: Climb) -> RunState:
        final.climb = climb
        if final.routing is not None:
            final.routing.escalation = climb.outcome.summary
            final.routing.reasons[:0] = climb.notes
        if final.trace is not None:
            final.trace.panel_models = final.panel_models
        return final


def _carried(state: RunState) -> Carried:
    """What ``state``'s rung produced, handed to the next rung: one member per answer (a spare
    that answered for a member is carried as itself)."""
    assert state.fanout is not None
    by_model = {m.model: m for m in state.members}
    return Carried(
        fanout=state.fanout,
        successful=list(state.successful),
        members=[by_model.get(model) or PanelMember(model=model) for model, _ in state.successful],
        answer_lenses=dict(state.answer_lenses),
        answer_approaches=dict(state.answer_approaches),
    )
