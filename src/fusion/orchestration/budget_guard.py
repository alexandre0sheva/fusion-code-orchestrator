"""Hard cost and latency caps: a forecast before the run, a guard during it.

``Strategy.max_cost_usd`` is enforced twice. Before any call, ``preflight`` prices the planned
calls (assumed token counts, see ``fusion.routing.budget``) and, when they would pass the cap,
shifts the strategy down step by step: drop refinement and judge calls, then shrink the panel,
then fall back to its cheapest member alone. If even that is over, the run is refused before any
model is called. During the run, ``BudgetGuard`` prices each optional stage (refinement, the
judge, synthesis, a cascade's escalation) against the money already spent and skips the stage
that would pass the cap; the run then returns what it already has. ``Strategy.max_latency_s`` is
enforced during the run only: no stage is started that, judging by the slowest call so far,
would finish after the cap.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from fusion.config.loader import ModelEntry
from fusion.orchestration.cascade import cheapest_first
from fusion.orchestration.ledger import RunLedger
from fusion.orchestration.strategy import Strategy
from fusion.routing.budget import (
    ANSWER_OUTPUT_TOKENS,
    JUDGE_OUTPUT_TOKENS,
    PROMPT_OVERHEAD_TOKENS,
    SYNTHESIS_OUTPUT_TOKENS,
    PlannedCall,
    RunForecast,
    forecast_calls,
)
from fusion.telemetry.cost import PricingRegistry

__all__ = [
    "BudgetGuard",
    "BudgetReport",
    "Preflight",
    "down_shifts",
    "escalation_calls",
    "judge_calls",
    "plan_calls",
    "preflight",
    "refine_round_calls",
    "synthesis_calls",
]

# What the synthesizer reads on top of the answers: the claim clusters, about half their size.
_CLUSTER_SHARE = 0.5


class BudgetReport(BaseModel):
    """What the caps did to a run; stored with it."""

    max_cost_usd: float | None = None
    max_latency_s: float | None = None
    requested_forecast_usd: float | None = None  # the strategy as asked for
    forecast_usd: float | None = None  # the strategy that ran
    forecast_known: bool = True
    shifts: list[str] = Field(default_factory=list)  # down-shifts applied before the run
    skipped: list[str] = Field(default_factory=list)  # stages skipped during the run


# ----------------------------------------------------------------------------- planning calls


def _call_input(prompt_tokens: int) -> int:
    return prompt_tokens + PROMPT_OVERHEAD_TOKENS


def panel_calls(aliases: Sequence[str], prompt_tokens: int) -> list[PlannedCall]:
    return [
        PlannedCall("panel", alias, _call_input(prompt_tokens), ANSWER_OUTPUT_TOKENS)
        for alias in aliases
    ]


def refine_round_calls(
    aliases: Sequence[str], prompt_tokens: int, answers: Sequence[int]
) -> list[PlannedCall]:
    """One refinement round: each member re-reads its peers' answers (all but its own)."""
    peers = sum(answers) - (sum(answers) // len(answers) if answers else 0)
    return [
        PlannedCall("refine", alias, _call_input(prompt_tokens) + peers, ANSWER_OUTPUT_TOKENS)
        for alias in aliases
    ]


def judge_calls(
    judge_model: str, prompt_tokens: int, answers: Sequence[int], *, full: bool
) -> list[PlannedCall]:
    """One judge call per answer, plus one check of the judge's own output for ``full``."""
    calls = [
        PlannedCall("judge", judge_model, _call_input(prompt_tokens) + tokens, JUDGE_OUTPUT_TOKENS)
        for tokens in answers
    ]
    if full and answers:
        calls.append(PlannedCall("eval", judge_model, JUDGE_OUTPUT_TOKENS * 2, JUDGE_OUTPUT_TOKENS))
    return calls


def synthesis_calls(
    model: str, prompt_tokens: int, answers: Sequence[int]
) -> list[PlannedCall]:
    """The synthesizer reads the task, every answer and the claim clusters."""
    read = _call_input(prompt_tokens) + sum(answers) + int(sum(answers) * _CLUSTER_SHARE)
    return [PlannedCall("synthesis", model, read, SYNTHESIS_OUTPUT_TOKENS)]


def _answer_sizes(count: int) -> list[int]:
    return [ANSWER_OUTPUT_TOKENS] * count


def plan_calls(
    strategy: Strategy,
    *,
    judge_model: str,
    prompt_tokens: int,
    include_escalation: bool = True,
) -> list[PlannedCall]:
    """Every call a run of ``strategy`` is expected to make.

    A cascade's members are taken in the order they will run. With ``include_escalation=False``
    only its first wave is planned (what the run commits to before knowing whether it escalates).
    """
    aliases = [m.model for m in strategy.members]
    cascade = strategy.cascade if strategy.kind == "cascade" else None
    if cascade is not None and not include_escalation:
        return panel_calls(aliases[: cascade.first], prompt_tokens)
    calls = panel_calls(aliases, prompt_tokens)
    answers = _answer_sizes(len(aliases))
    for _ in range(strategy.rounds - 1):
        calls += refine_round_calls(aliases, prompt_tokens, answers)
    if strategy.judge != "off":
        calls += judge_calls(judge_model, prompt_tokens, answers, full=strategy.judge == "full")
    if strategy.kind != "solo" and strategy.aggregator == "llm" and strategy.aggregator_model:
        calls += synthesis_calls(strategy.aggregator_model, prompt_tokens, answers)
    return calls


def escalation_calls(
    strategy: Strategy, *, prompt_tokens: int, first_wave: Sequence[int]
) -> list[PlannedCall]:
    """The rest of a cascade's panel and, if it synthesizes, the synthesis over every answer."""
    assert strategy.cascade is not None
    rest = [m.model for m in strategy.members][strategy.cascade.first :]
    calls = panel_calls(rest, prompt_tokens)
    answers = [*first_wave, *_answer_sizes(len(rest))]
    if strategy.aggregator == "llm" and strategy.aggregator_model:
        calls += synthesis_calls(strategy.aggregator_model, prompt_tokens, answers)
    return calls


# ------------------------------------------------------------------------------- down-shifting


def down_shifts(
    plan: Strategy, models: dict[str, ModelEntry], pricing: PricingRegistry
) -> Iterator[tuple[str, Strategy]]:
    """Ever cheaper versions of ``plan`` with a label for each step: refinement and judge calls
    go first, then the dearest members one at a time down to two, then the cheapest alone."""
    current = plan
    if plan.kind == "panel" and (plan.rounds > 1 or plan.judge != "off"):
        dropped = " and ".join(
            what
            for what, present in (
                ("refinement", plan.rounds > 1),
                ("judge calls", plan.judge != "off"),
            )
            if present
        )
        current = current.model_copy(
            update={"rounds": 1, "judge": "off", "judge_feeds_synthesis": False}
        )
        yield f"dropped {dropped}", current
    ranked = cheapest_first(plan.members, models, pricing)
    order = {m.model: i for i, m in enumerate(ranked)}
    if plan.kind == "panel":
        while len(current.members) > 2:
            dearest = max(current.members, key=lambda m: order[m.model])
            kept = [m for m in current.members if m is not dearest]
            current = current.model_copy(update={"members": kept})
            yield f"dropped {dearest.model} from the panel", current
    if plan.kind != "solo":
        cheapest = ranked[0]
        solo = current.model_copy(
            update={
                "kind": "solo",
                "members": [cheapest],
                "rounds": 1,
                "judge": "off",
                "judge_feeds_synthesis": False,
                "aggregator_model": None,
                "cascade": None,
            }
        )
        yield f"fell back to {cheapest.model} alone", solo


@dataclass
class Preflight:
    """The strategy to run, what it is forecast to cost, and what had to give."""

    strategy: Strategy
    forecast: RunForecast
    requested: RunForecast
    shifts: list[str] = field(default_factory=list)
    within_cap: bool | None = True  # None: the cost could not be forecast, so the cap is unchecked


def preflight(
    plan: Strategy,
    *,
    models: dict[str, ModelEntry],
    pricing: PricingRegistry,
    judge_model: str,
    prompt_tokens: int,
) -> Preflight:
    """Forecast ``plan`` against its ``max_cost_usd`` and shift it down until it fits.

    ``plan`` must name the members and aggregator that will actually run. A cascade is forecast
    for its first wave only; the mid-run guard decides whether it may escalate.
    """
    if plan.kind == "cascade":
        plan = plan.model_copy(update={"members": cheapest_first(plan.members, models, pricing)})

    def forecast(strategy: Strategy) -> RunForecast:
        calls = plan_calls(
            strategy,
            judge_model=judge_model,
            prompt_tokens=prompt_tokens,
            include_escalation=False,
        )
        return forecast_calls(calls, models, pricing)

    first = forecast(plan)
    cap = plan.max_cost_usd
    if cap is None:
        return Preflight(plan, first, first)
    if first.usd <= cap:  # a lower bound under the cap proves nothing when a price is missing
        return Preflight(plan, first, first, within_cap=True if first.known else None)
    chosen, current, shifts = first, plan, []
    for label, shifted in down_shifts(plan, models, pricing):
        current, chosen = shifted, forecast(shifted)
        shifts.append(label)
        if chosen.usd <= cap:
            fits = True if chosen.known else None
            return Preflight(current, chosen, first, shifts, within_cap=fits)
    return Preflight(current, chosen, first, shifts, within_cap=False)


# --------------------------------------------------------------------------------- the guard


class BudgetGuard:
    """Checks optional stages against the money and time a run has left."""

    def __init__(
        self,
        *,
        max_cost_usd: float | None,
        max_latency_s: float | None,
        ledger: RunLedger,
        clock: Callable[[], float],
        started: float,
        models: dict[str, ModelEntry],
        pricing: PricingRegistry,
    ) -> None:
        self.max_cost_usd = max_cost_usd
        self.max_latency_s = max_latency_s
        self._ledger = ledger
        self._clock = clock
        self._started = started
        self._models = models
        self._pricing = pricing
        self._reserved: dict[str, float] = {}
        self.skipped: list[str] = []

    @property
    def active(self) -> bool:
        return self.max_cost_usd is not None or self.max_latency_s is not None

    def spent_usd(self) -> float:
        return self._ledger.total_cost().usd

    def elapsed_s(self) -> float:
        return self._clock() - self._started

    def slowest_call_s(self) -> float:
        """The slowest call so far: the guess for how long the next stage will take."""
        done = [r.latency_ms for r in self._ledger.ordered_records() if r.ok]
        return max(done, default=0.0) / 1000

    def check(self, stage: str, calls: Sequence[PlannedCall]) -> str | None:
        """None when ``calls`` fit under both caps (the money is then held until ``release``);
        otherwise a sentence saying which cap they would pass."""
        if not self.active:
            return None
        reason = self._cost_problem(stage, calls) or self._latency_problem(stage)
        if reason:
            self.skipped.append(stage)
            return reason
        return None

    def _cost_problem(self, stage: str, calls: Sequence[PlannedCall]) -> str | None:
        if self.max_cost_usd is None:
            return None
        estimate = forecast_calls(calls, self._models, self._pricing)
        held = sum(self._reserved.values())
        committed = self.spent_usd() + held
        if committed + estimate.usd > self.max_cost_usd:
            return (
                f"Cost cap: {stage} would cost about ${estimate.usd:.4f} on top of "
                f"${committed:.4f} already spent or committed, over the "
                f"${self.max_cost_usd:.4f} cap"
            )
        self._reserved[stage] = estimate.usd
        return None

    def _latency_problem(self, stage: str) -> str | None:
        if self.max_latency_s is None:
            return None
        needed = self.slowest_call_s()
        if needed > 0 and self.elapsed_s() + needed > self.max_latency_s:
            self._reserved.pop(stage, None)
            return (
                f"Latency cap: {stage} would take about {needed:.1f}s with "
                f"{self.elapsed_s():.1f}s of the {self.max_latency_s:.1f}s cap used"
            )
        return None

    def release(self, stage: str) -> None:
        """The stage's calls are in the ledger now, so stop holding their estimated cost."""
        self._reserved.pop(stage, None)
