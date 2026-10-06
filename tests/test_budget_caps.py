"""Hard caps: the forecast before a run, down-shifting, and the guard between stages."""

from __future__ import annotations

from pathlib import Path

import pytest

from _scripted import (
    AGREED,
    FAST,
    JUDGE,
    OTHER,
    PROMPT,
    SECURITY,
    UNRELATED,
    WEAK,
    Scripted,
    context,
    pipeline,
    priced_catalog,
    stages,
)
from fusion.orchestration.budget_guard import (
    BudgetGuard,
    down_shifts,
    escalation_calls,
    judge_calls,
    plan_calls,
    preflight,
    refine_round_calls,
    synthesis_calls,
)
from fusion.orchestration.ledger import CallRecord, RunLedger
from fusion.orchestration.strategy import Strategy, load_strategy_book, mock_strategy_book
from fusion.routing.budget import PlannedCall, estimate_tokens, forecast_calls
from fusion.routing.model_registry import ModelRegistry
from fusion.telemetry.cost import PricingRegistry

pytestmark = pytest.mark.usefixtures("three_model_default")

TOKENS = estimate_tokens(PROMPT)
CATALOG = priced_catalog()
PRICING = PricingRegistry(CATALOG)


def _strategy(name: str) -> Strategy:
    """A packaged strategy as offline mode runs it: on the mock models."""
    return mock_strategy_book(load_strategy_book(), ModelRegistry.for_mode(use_mock=True)).get(name)


def _usd(strategy: Strategy, *, escalate: bool = True) -> float:
    calls = plan_calls(
        strategy, judge_model=JUDGE, prompt_tokens=TOKENS, include_escalation=escalate
    )
    return forecast_calls(calls, CATALOG.models, PRICING).usd


def _fit(strategy: Strategy, cap: float | None) -> object:
    return preflight(
        strategy.model_copy(update={"max_cost_usd": cap}),
        models=CATALOG.models,
        pricing=PRICING,
        judge_model=JUDGE,
        prompt_tokens=TOKENS,
    )


# ------------------------------------------------------------------------------------- forecasts


def test_a_forecast_prices_each_call_from_the_catalog() -> None:
    call = PlannedCall("panel", FAST, input_tokens=1_000, output_tokens=2_000)
    forecast = forecast_calls([call], CATALOG.models, PRICING)
    assert forecast.usd == pytest.approx(1_000 / 1e6 * 1.0 + 2_000 / 1e6 * 5.0)
    assert forecast.known and forecast.unpriced == ()


def test_a_forecast_says_which_models_it_could_not_price() -> None:
    unpriced = priced_catalog({SECURITY: None})
    calls = [PlannedCall("panel", a, 1_000, 1_000) for a in (FAST, SECURITY, "no-such-model")]
    forecast = forecast_calls(calls, unpriced.models, PricingRegistry(unpriced))
    assert not forecast.known and forecast.unpriced == (SECURITY, "no-such-model")
    assert forecast.usd == pytest.approx(0.006)  # a lower bound: only the priced call counts


@pytest.mark.parametrize(
    ("name", "changes", "expected"),
    [
        ("solo-cheap", {}, ["panel"]),
        ("panel-cheap", {}, ["panel"] * 3 + ["synthesis"]),
        ("panel-digest", {}, ["panel"] * 3),
        ("panel-vote", {}, ["panel"] * 3),
        ("panel-refine", {}, ["panel"] * 3 + ["refine"] * 3 + ["synthesis"]),
        ("panel-cheap", {"judge": "light"}, ["panel"] * 3 + ["judge"] * 3 + ["synthesis"]),
        ("panel-cheap", {"judge": "full"}, ["panel"] * 3 + ["judge"] * 3 + ["eval", "synthesis"]),
        ("panel-cascade", {}, ["panel"] * 3 + ["synthesis"]),
    ],
)
def test_the_plan_lists_the_calls_a_strategy_makes(
    name: str, changes: dict[str, str], expected: list[str]
) -> None:
    plan = _strategy(name).model_copy(update=changes)
    calls = plan_calls(plan, judge_model=JUDGE, prompt_tokens=TOKENS)
    assert [c.stage for c in calls] == expected


def test_a_cascade_is_forecast_for_its_first_wave_only() -> None:
    cascade = _strategy("panel-cascade")
    first_wave = plan_calls(
        cascade, judge_model=JUDGE, prompt_tokens=TOKENS, include_escalation=False
    )
    assert [c.alias for c in first_wave] == [FAST, SECURITY]
    assert _usd(cascade, escalate=False) < _usd(cascade, escalate=True)


def test_later_calls_are_forecast_from_what_they_will_read() -> None:
    [refine_one, *_] = refine_round_calls([FAST, SECURITY, WEAK], 100, [1_000, 1_000, 1_000])
    [synthesis] = synthesis_calls(JUDGE, 100, [1_000, 1_000, 1_000])
    assert refine_one.input_tokens == 100 + 900 + 2_000  # two peers' answers
    assert synthesis.input_tokens == 100 + 900 + 3_000 + 1_500  # all answers and the clusters
    assert judge_calls(JUDGE, 100, [1_000], full=True)[-1].stage == "eval"
    escalation = escalation_calls(
        _strategy("panel-cascade").model_copy(update={"aggregator_model": JUDGE}),
        prompt_tokens=100,
        first_wave=[1_000, 1_000],
    )
    assert [c.stage for c in escalation] == ["panel", "synthesis"]


# --------------------------------------------------------------------------------- down-shifting


def test_the_ladder_drops_refinement_then_members_then_goes_solo() -> None:
    steps = list(down_shifts(_strategy("panel-refine"), CATALOG.models, PRICING))
    assert [label for label, _ in steps] == [
        "dropped refinement",
        f"dropped {SECURITY} from the panel",  # the dearest member goes first
        f"fell back to {WEAK} alone",  # the cheapest member stays
    ]
    costs = [_usd(s) for _, s in steps]
    assert costs == sorted(costs, reverse=True)
    solo = steps[-1][1]
    assert (solo.kind, solo.rounds, solo.judge, solo.aggregator_model) == ("solo", 1, "off", None)


def test_the_ladder_for_a_judged_panel_also_drops_the_judge() -> None:
    plan = _strategy("panel-cheap").model_copy(update={"judge": "light"})
    [(label, step), *_] = down_shifts(plan, CATALOG.models, PRICING)
    assert label == "dropped judge calls" and step.judge == "off"


def test_a_cascade_can_only_fall_back_to_one_model() -> None:
    steps = list(down_shifts(_strategy("panel-cascade"), CATALOG.models, PRICING))
    assert [label for label, _ in steps] == [f"fell back to {WEAK} alone"]


def test_a_solo_strategy_has_nowhere_to_go() -> None:
    assert list(down_shifts(_strategy("solo-cheap"), CATALOG.models, PRICING)) == []


def test_preflight_keeps_a_strategy_that_fits_and_ignores_a_missing_cap() -> None:
    refine = _strategy("panel-refine")
    for cap in (None, _usd(refine) + 0.001):
        found = _fit(refine, cap)
        assert found.shifts == [] and found.strategy.rounds == 2 and found.within_cap  # type: ignore[attr-defined]


def test_preflight_shifts_just_far_enough() -> None:
    refine = _strategy("panel-refine")
    rungs = [s for _, s in down_shifts(refine, CATALOG.models, PRICING)]
    shapes = []
    for rung in rungs:
        found = _fit(refine, _usd(rung) + 1e-9)
        shapes.append((found.shifts, found.strategy.kind, len(found.strategy.members)))  # type: ignore[attr-defined]
        assert found.within_cap is True  # type: ignore[attr-defined]
    assert shapes == [
        (["dropped refinement"], "panel", 3),
        (["dropped refinement", f"dropped {SECURITY} from the panel"], "panel", 2),
        (
            [
                "dropped refinement",
                f"dropped {SECURITY} from the panel",
                f"fell back to {WEAK} alone",
            ],
            "solo",
            1,
        ),
    ]


def test_preflight_gives_up_when_even_the_cheapest_option_is_over() -> None:
    refine = _strategy("panel-refine")
    cheapest = [s for _, s in down_shifts(refine, CATALOG.models, PRICING)][-1]
    found = _fit(refine, _usd(cheapest) / 2)
    assert found.within_cap is False  # type: ignore[attr-defined]
    assert found.strategy.kind == "solo"  # type: ignore[attr-defined]


def test_preflight_cannot_check_a_cap_when_a_model_has_no_price() -> None:
    unpriced = priced_catalog({SECURITY: None})
    found = preflight(
        _strategy("panel-cheap").model_copy(update={"max_cost_usd": 10.0}),
        models=unpriced.models,
        pricing=PricingRegistry(unpriced),
        judge_model=JUDGE,
        prompt_tokens=TOKENS,
    )
    assert found.within_cap is None and found.forecast.unpriced == (SECURITY,)


# --------------------------------------------------------------------------------- the guard


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _guard(
    *, cost: float | None = None, seconds: float | None = None
) -> tuple[BudgetGuard, RunLedger, Clock]:
    clock = Clock()
    ledger = RunLedger(clock)
    guard = BudgetGuard(
        max_cost_usd=cost,
        max_latency_s=seconds,
        ledger=ledger,
        clock=clock,
        started=0.0,
        models=CATALOG.models,
        pricing=PRICING,
    )
    return guard, ledger, clock


def _record(ledger: RunLedger, *, usd: float = 0.0, ms: float = 0.0, stage: str = "panel") -> None:
    record = CallRecord(
        stage=stage, model_alias=FAST, provider="mock", cost_usd=usd, latency_ms=ms  # type: ignore[arg-type]
    )
    ledger.add(record)


def test_a_guard_without_caps_allows_everything() -> None:
    guard, ledger, _ = _guard()
    _record(ledger, usd=100.0, ms=1e6)
    assert not guard.active
    assert guard.check("synthesis", [PlannedCall("synthesis", JUDGE, 10**6, 10**6)]) is None
    assert guard.skipped == []


def test_the_guard_counts_money_already_spent() -> None:
    guard, ledger, _ = _guard(cost=0.05)
    _record(ledger, usd=0.03)
    one_call = [PlannedCall("synthesis", FAST, 1_000, 1_000)]  # about $0.006
    assert guard.check("synthesis", one_call) is None
    guard.release("synthesis")
    _record(ledger, usd=0.02)
    reason = guard.check("synthesis", one_call)
    assert reason is not None and reason.startswith("Cost cap: synthesis would cost about $0.006")
    assert guard.skipped == ["synthesis"]


def test_stages_that_run_together_hold_their_money_until_released() -> None:
    guard, _, _ = _guard(cost=0.010)
    call = [PlannedCall("judge", FAST, 1_000, 1_000)]  # about $0.006: fits once, not twice
    assert guard.check("judge", call) is None
    assert guard.check("synthesis", call) is not None
    guard.release("judge")
    assert guard.check("synthesis", call) is None


def test_the_guard_skips_a_stage_that_would_finish_after_the_latency_cap() -> None:
    guard, ledger, clock = _guard(seconds=3.0)
    assert guard.check("refinement", []) is None  # nothing has run, so nothing is known
    _record(ledger, ms=2_000)
    clock.now = 1.5
    reason = guard.check("refinement", [])
    assert reason is not None and "Latency cap: refinement would take about 2.0s" in reason
    clock.now = 0.5
    assert guard.check("refinement", []) is None


# ------------------------------------------------------------------ before the run (pipeline)


def _cap(monkeypatch: pytest.MonkeyPatch, strategy: str, **fields: float) -> None:
    for name, value in fields.items():
        monkeypatch.setenv(f"FUSION__STRATEGIES__{strategy.upper()}__{name.upper()}", str(value))


async def test_a_run_over_its_cap_is_shifted_down_with_a_visible_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refine = _strategy("panel-refine")
    _cap(monkeypatch, "panel-refine", max_cost_usd=_usd(refine) - 0.01)
    provider = Scripted()
    result = await pipeline(tmp_path, provider, priced=True).run(context(strategy="panel-refine"))
    assert stages(result) == ["panel"] * 3 + ["synthesis"]  # no refinement
    assert result.budget is not None and result.budget.shifts == ["dropped refinement"]
    assert result.budget.requested_forecast_usd == pytest.approx(_usd(refine))
    assert result.budget.forecast_usd < result.budget.requested_forecast_usd  # type: ignore[operator]
    assert any("was forecast at" in w and "dropped refinement" in w for w in result.warnings)
    assert "Cost cap: dropped refinement" in result.routing.reasons


async def test_a_tight_cap_runs_the_cheapest_model_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cap(monkeypatch, "panel-cheap", max_cost_usd=0.005)
    provider = Scripted()
    result = await pipeline(tmp_path, provider, priced=True).run(context())
    assert provider.models_called() == [WEAK]
    assert stages(result) == ["panel"]
    assert result.routing.selected_panel == [WEAK] and result.routing.synthesizer_model == ""
    assert result.trace.panel_models == [WEAK]
    assert result.budget is not None and result.budget.shifts[-1] == f"fell back to {WEAK} alone"


async def test_a_cap_below_the_cheapest_option_refuses_the_run_before_any_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cap(monkeypatch, "panel-cheap", max_cost_usd=0.0001)
    provider = Scripted()
    pipe = pipeline(tmp_path, provider, priced=True)
    result = await pipe.run(context())
    assert provider.requests == [] and result.ledger.records == []
    assert result.final_answer.startswith("Cost cap not met: the cheapest way to run 'panel-cheap'")
    assert "strategies.panel-cheap.max_cost_usd" in result.final_answer
    assert result.total_cost_usd == 0.0
    assert result.structured_output["budget_exceeded"] is True
    stored = pipe.deps.run_store.get_run(result.run_id)
    assert stored is not None and stored.output_data["budget"]["max_cost_usd"] == 0.0001


async def test_a_run_without_a_cap_reports_no_budget(tmp_path: Path) -> None:
    result = await pipeline(tmp_path, Scripted(), priced=True).run(context())
    assert result.budget is None and not any("cap" in w.lower() for w in result.warnings)


async def test_a_cap_that_cannot_be_checked_up_front_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cap(monkeypatch, "panel-cheap", max_cost_usd=10.0)
    pipe = pipeline(tmp_path, Scripted(), pricing=PricingRegistry(priced_catalog({SECURITY: None})))
    result = await pipe.run(context())
    assert any(f"cannot be checked up front: no price for {SECURITY}" in w for w in result.warnings)
    assert result.budget is not None and result.budget.forecast_known is False


async def test_a_run_that_still_goes_over_its_cap_is_flagged_after_the_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cap(monkeypatch, "solo-cheap", max_cost_usd=0.01)  # forecast about $0.008: passes
    provider = Scripted(cost={"*": 0.02})  # but the call really costs $0.02
    result = await pipeline(tmp_path, provider, priced=True).run(context(strategy="solo-cheap"))
    assert any("exceeded its cap" in w for w in result.warnings)


# --------------------------------------------------------------------- during the run (pipeline)


async def test_synthesis_that_would_pass_the_cap_becomes_a_free_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel = _usd(_strategy("panel-cheap"))
    _cap(monkeypatch, "panel-cheap", max_cost_usd=panel + 0.001)  # the forecast fits
    provider = Scripted(cost={"panel": 0.03})  # but the three panel calls cost $0.09
    result = await pipeline(tmp_path, provider, priced=True).run(context())
    assert stages(result) == ["panel"] * 3  # synthesis never started
    assert result.final_answer.startswith("## Panel digest: 3 answers")
    assert any(w.startswith("Cost cap: synthesis would cost") for w in result.warnings)
    assert result.budget is not None and result.budget.skipped == ["synthesis"]


async def test_refinement_that_would_pass_the_cap_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cap(monkeypatch, "panel-refine", max_cost_usd=_usd(_strategy("panel-refine")) + 0.001)
    provider = Scripted({FAST: AGREED, SECURITY: OTHER, WEAK: UNRELATED}, cost={"panel": 0.05})
    result = await pipeline(tmp_path, provider, priced=True).run(context(strategy="panel-refine"))
    assert "refine" not in stages(result)
    assert any(w.startswith("Cost cap: refinement") and "skipped" in w for w in result.warnings)
    assert result.budget is not None and result.budget.skipped[0] == "refinement"


async def test_the_judge_is_skipped_when_it_does_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cap(monkeypatch, "panel-vote", max_cost_usd=0.06)
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-VOTE__JUDGE", "light")
    provider = Scripted(cost={"panel": 0.019})
    result = await pipeline(tmp_path, provider, priced=True).run(context(strategy="panel-vote"))
    assert "judge" not in stages(result)
    assert any("scored by deterministic checks only" in w for w in result.warnings)


async def test_a_latency_cap_skips_stages_that_would_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cap(monkeypatch, "panel-refine", max_latency_s=0.15)
    provider = Scripted(delay={"panel": 0.1})
    result = await pipeline(tmp_path, provider).run(context(strategy="panel-refine"))
    assert stages(result) == ["panel"] * 3  # no refinement, no synthesis
    assert any(w.startswith("Latency cap: refinement") for w in result.warnings)
    assert result.final_answer.startswith("## Panel digest")


async def test_a_cascade_that_cannot_afford_to_escalate_returns_its_first_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cascade = _strategy("panel-cascade")
    _cap(monkeypatch, "panel-cascade", max_cost_usd=_usd(cascade, escalate=False) + 0.001)
    provider = Scripted({FAST: AGREED, SECURITY: OTHER, WEAK: UNRELATED})
    result = await pipeline(tmp_path, provider, priced=True).run(context(strategy="panel-cascade"))
    assert provider.models_called("panel") == [WEAK, FAST]  # the two cheapest, then nobody
    assert result.cascade is not None and result.cascade.exited_early
    assert result.cascade.stopped_by_budget
    assert any("the cascade did not escalate" in w for w in result.warnings)
    assert "synthesis" not in stages(result)
    assert result.final_answer  # something is returned: no majority, so the best answer


async def test_a_cascade_with_room_in_the_budget_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cascade = _strategy("panel-cascade")
    _cap(monkeypatch, "panel-cascade", max_cost_usd=_usd(cascade) + 0.05)
    provider = Scripted({FAST: AGREED, SECURITY: OTHER, WEAK: UNRELATED})
    result = await pipeline(tmp_path, provider, priced=True).run(context(strategy="panel-cascade"))
    assert result.cascade is not None and not result.cascade.exited_early
    assert not result.cascade.stopped_by_budget
