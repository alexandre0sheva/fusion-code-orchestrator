"""Parallelism and latency: what overlaps, what waits, and what is cancelled.

Providers here have scripted latencies, and assertions compare each call's start and end offsets
from the run ledger (never wall-clock sleeps of the test itself), with wide margins.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from fusion.config.catalog import load_catalog
from fusion.config.layers import ConfigError
from fusion.config.loader import EarlyReturn, FanoutConfig, ModelEntry, load_routing_policies
from fusion.orchestration.context import PipelineContext
from fusion.orchestration.factory import Settings, build_pipeline
from fusion.orchestration.fanout import fanout_to_panel
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.orchestration.strategy import load_strategy_book
from fusion.providers.base import ModelRequest, ModelResponse, ProviderError
from fusion.providers.limits import ProviderLimiter
from fusion.providers.mock import MockProvider
from fusion.routing.classifier import TaskType
from fusion.telemetry.cost import PricingRegistry

Delay = Callable[[ModelRequest], float]
LONG = "Retries around a flaky HTTP call keep timing out in production under load."


class Scripted(MockProvider):
    """Mock answers after a scripted delay; remembers how many calls were in flight at once."""

    def __init__(self, delay: Delay | dict[str, float] | float = 0.0) -> None:
        super().__init__(latency_ms=0.0)
        if isinstance(delay, dict):
            table = delay
            delay = lambda r: table.get(  # noqa: E731
                str(r.metadata.get("role")), table.get(r.model_id, 0.0)
            )
        elif isinstance(delay, float):
            fixed = delay
            delay = lambda _r: fixed  # noqa: E731
        self._delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self._delay(request))
            return await super().complete(request)
        finally:
            self.in_flight -= 1


def _pipe(tmp_path: Path, provider: MockProvider, **kw: Any) -> Any:
    settings = Settings(use_mock=True, db_path=str(tmp_path / "c.db"), **kw)
    return build_pipeline(settings, {"mock": provider})


def _ctx(**kw: Any) -> PipelineContext:
    return PipelineContext(task_type=TaskType.CODE_REVIEW, primary_content=LONG, **kw)


def _spans(result: Any, stage: str) -> list[dict[str, Any]]:
    return [s for s in result.ledger.timeline() if s["stage"] == stage]


def _starts(spans: list[dict[str, Any]]) -> list[float]:
    return [float(s["start_ms"]) for s in spans]


def _ends(spans: list[dict[str, Any]]) -> list[float]:
    return [float(s["end_ms"]) for s in spans]


# ------------------------------------------------------------------------------ the panel


async def test_a_three_model_panel_takes_as_long_as_its_slowest_model_plus_synthesis(
    tmp_path: Path,
) -> None:
    delays = {"mock-fast": 0.12, "mock-security": 0.18, "mock-weak": 0.24, "synthesizer": 0.06}
    result = await _pipe(tmp_path, Scripted(delays)).run(_ctx(strategy="panel-cheap"))
    panel, synthesis = _spans(result, "panel"), _spans(result, "synthesis")
    assert max(_starts(panel)) - min(_starts(panel)) < 100  # all three start together
    assert 230 <= result.fanout.panel_wall_latency_ms < 330  # the slowest, not 0.54s summed
    assert synthesis[0]["start_ms"] >= max(_ends(panel)) - 5  # synthesis needs every answer
    assert 290 <= result.total_latency_ms < 450  # slowest 240ms + synthesis 60ms, not 480ms


async def test_providers_do_not_starve_each_other_under_a_small_concurrency_cap() -> None:
    """``max_concurrency`` applies per provider: a busy slow provider cannot hold up a fast one."""
    base = load_catalog().models["mock-fast"]
    models: dict[str, ModelEntry] = {
        "slow-1": base.model_copy(
            update={"alias": "slow-1", "model_id": "slow-1", "provider": "slowp"}
        ),
        "slow-2": base.model_copy(
            update={"alias": "slow-2", "model_id": "slow-2", "provider": "slowp"}
        ),
        "fast-1": base.model_copy(
            update={"alias": "fast-1", "model_id": "fast-1", "provider": "fastp"}
        ),
    }

    class Named(Scripted):
        def __init__(self, name: str, delay: float) -> None:
            super().__init__(delay)
            self.name = name

    providers = {"slowp": Named("slowp", 0.15), "fastp": Named("fastp", 0.02)}
    gateway = CallGateway(
        ledger=RunLedger(),
        models=models,
        providers=providers,
        pricing=PricingRegistry(),  # type: ignore[arg-type]
    )
    result = await fanout_to_panel(
        panel_models=["slow-1", "slow-2", "fast-1"],
        registry_models=models,
        providers=providers,  # type: ignore[arg-type]
        task_type=TaskType.DEBUGGING,
        primary_content=LONG,
        config=FanoutConfig(max_concurrency=1, min_successful_responses=1),
        gateway=gateway,
    )
    spans = {s["model"]: s for s in gateway.ledger.timeline()}
    assert result.success_count == 3
    assert spans["fast-1"]["end_ms"] < 100  # not queued behind either slow call
    assert spans["slow-2"]["start_ms"] >= spans["slow-1"]["end_ms"] - 5  # same provider: serial
    assert providers["slowp"].max_in_flight == 1  # type: ignore[attr-defined]


async def test_a_provider_limiter_caps_in_flight_calls_across_the_panel() -> None:
    base = load_catalog().models["mock-fast"]
    models = {
        f"m{i}": base.model_copy(update={"alias": f"m{i}", "model_id": f"m{i}", "provider": "lim"})
        for i in range(4)
    }
    limiter = ProviderLimiter(max_concurrent=2)

    class Limited(Scripted):
        name = "lim"

        async def complete(self, request: ModelRequest) -> ModelResponse:
            async with limiter.slot():
                return await super().complete(request)

    provider = Limited(0.08)
    gateway = CallGateway(
        ledger=RunLedger(), models=models, providers={"lim": provider}, pricing=PricingRegistry()
    )
    started = time.perf_counter()
    result = await fanout_to_panel(
        panel_models=list(models),
        registry_models=models,
        providers={"lim": provider},
        task_type=TaskType.DEBUGGING,
        primary_content=LONG,
        config=FanoutConfig(max_concurrency=6, min_successful_responses=1),
        gateway=gateway,
    )
    assert provider.max_in_flight == 2  # the limiter, not the fan-out, holds the line
    assert result.success_count == 4
    assert 0.15 <= time.perf_counter() - started < 0.4  # two waves of two


# --------------------------------------------------------------------------- early return


def _early(monkeypatch: pytest.MonkeyPatch, quorum: int, grace_ms: float) -> None:
    monkeypatch.setenv("FUSION__FANOUT__EARLY_RETURN__QUORUM", str(quorum))
    monkeypatch.setenv("FUSION__FANOUT__EARLY_RETURN__GRACE_MS", str(grace_ms))


async def test_early_return_cancels_stragglers_and_records_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _early(monkeypatch, quorum=2, grace_ms=20)
    delays = {"mock-fast": 0.05, "mock-security": 0.06, "mock-weak": 3.0}
    started = time.perf_counter()
    result = await _pipe(tmp_path, Scripted(delays)).run(_ctx(strategy="panel-cheap"))
    assert time.perf_counter() - started < 1.0  # did not wait three seconds
    assert result.fanout.early_return and result.fanout.success_count == 2
    straggler = next(c for c in result.fanout.calls if c.model_name == "mock-weak")
    assert straggler.status == "cancelled" and "early return" in (straggler.error or "")
    record = next(r for r in result.ledger.records if r.model_alias == "mock-weak")
    assert record.status == "cancelled" and not record.cost_known  # may have been billed
    assert any("Early return" in w for w in result.warnings)
    assert "synthesis" in [r.stage for r in result.ledger.records]  # the run went on
    assert {p.model_name for p in result.panel_results} == {"mock-fast", "mock-security"}


async def test_stragglers_inside_the_grace_window_still_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _early(monkeypatch, quorum=2, grace_ms=600)
    delays = {"mock-fast": 0.03, "mock-security": 0.04, "mock-weak": 0.15}
    result = await _pipe(tmp_path, Scripted(delays)).run(_ctx(strategy="panel-cheap"))
    assert result.fanout.success_count == 3 and not result.fanout.early_return


async def test_early_return_never_stops_before_the_minimum_successful_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _early(monkeypatch, quorum=1, grace_ms=0)  # asks for 1, but min_successful_responses is 2
    delays = {"mock-fast": 0.02, "mock-security": 0.12, "mock-weak": 3.0}
    result = await _pipe(tmp_path, Scripted(delays)).run(_ctx(strategy="panel-cheap"))
    assert {p.model_name for p in result.panel_results} == {"mock-fast", "mock-security"}


def test_fanout_latency_options_validate_and_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    assert FanoutConfig().early_return is None and FanoutConfig().hedge_after_ms is None
    assert EarlyReturn().quorum == 2 and EarlyReturn().grace_ms == 1500
    _early(monkeypatch, quorum=3, grace_ms=250)
    monkeypatch.setenv("FUSION__FANOUT__HEDGE_AFTER_MS", "8000")
    fanout = load_routing_policies().fanout
    assert fanout.early_return == EarlyReturn(quorum=3, grace_ms=250)
    assert fanout.hedge_after_ms == 8000
    monkeypatch.setenv("FUSION__FANOUT__EARLY_RETURN__QUORUM", "0")
    with pytest.raises(ConfigError, match="fanout.early_return.quorum = 0"):
        load_routing_policies()
    monkeypatch.setenv("FUSION__FANOUT__EARLY_RETURN__QUORUM", "2")
    monkeypatch.setenv("FUSION__FANOUT__HEDGE_AFTER_MS", "0")
    with pytest.raises(ConfigError, match="hedge_after_ms"):
        load_routing_policies()


# ------------------------------------------------------------------------------- hedging


async def test_a_slow_member_is_hedged_to_another_panel_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__FANOUT__HEDGE_AFTER_MS", "60")
    delays = {"mock-fast": 3.0, "mock-security": 0.02}
    started = time.perf_counter()
    result = await _pipe(tmp_path, Scripted(delays)).run(_ctx(strategy="solo-cheap"))
    assert time.perf_counter() - started < 1.0
    assert result.fanout.hedged == {"mock-fast": "mock-security"}
    assert [p.model_name for p in result.panel_results] == ["mock-security"]  # who really answered
    panel = {r.model_alias: r for r in result.ledger.records if r.stage == "panel"}
    assert panel["mock-security"].status == "success"
    assert panel["mock-fast"].status == "cancelled" and not panel["mock-fast"].cost_known
    assert any("was slow; asked mock-security as well" in w for w in result.warnings)
    assert panel["mock-security"].started_at_ms >= 55  # only after the wait


async def test_no_hedge_when_the_member_answers_in_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__FANOUT__HEDGE_AFTER_MS", "500")
    result = await _pipe(tmp_path, Scripted(0.02)).run(_ctx(strategy="solo-cheap"))
    assert result.fanout.hedged == {}
    assert len([r for r in result.ledger.records if r.stage == "panel"]) == 1


async def test_a_hedge_with_nobody_left_to_ask_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__FANOUT__HEDGE_AFTER_MS", "40")
    delays = {"mock-fast": 0.2, "mock-security": 0.01, "mock-weak": 0.01}
    result = await _pipe(tmp_path, Scripted(delays)).run(_ctx(strategy="panel-cheap"))
    assert any("no other panel model was available" in w for w in result.warnings)
    assert result.fanout.success_count == 3  # the slow one simply finished


# ---------------------------------------------------------------------------------- judge


def _judge(monkeypatch: pytest.MonkeyPatch, *, feeds: bool = False) -> None:
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CHEAP__JUDGE", "light")
    if feeds:
        monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CHEAP__JUDGE_FEEDS_SYNTHESIS", "true")


async def test_per_answer_judge_calls_run_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _judge(monkeypatch)
    result = await _pipe(tmp_path, Scripted({"judge": 0.1})).run(_ctx(strategy="panel-cheap"))
    judges = _spans(result, "judge")
    assert len(judges) == 3
    assert max(_starts(judges)) - min(_starts(judges)) < 70
    assert max(_ends(judges)) - min(_starts(judges)) < 220  # one wave, not three in a row


async def test_the_judge_runs_alongside_synthesis_when_synthesis_does_not_need_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _judge(monkeypatch)
    delays = {"judge": 0.25, "synthesizer": 0.2}
    result = await _pipe(tmp_path, Scripted(delays)).run(_ctx(strategy="panel-cheap"))
    synthesis = _spans(result, "synthesis")[0]
    judges = _spans(result, "judge")
    assert synthesis["start_ms"] < min(_ends(judges))  # they overlap
    assert min(_starts(judges)) < synthesis["end_ms"]
    assert abs(synthesis["start_ms"] - min(_starts(judges))) < 90
    assert result.total_latency_ms < 420  # about the longer of the two, not 250 + 200


async def test_synthesis_waits_for_the_judge_when_it_reads_its_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _judge(monkeypatch, feeds=True)
    spy = Scripted({"judge": 0.15, "synthesizer": 0.05})
    result = await _pipe(tmp_path, spy).run(_ctx(strategy="panel-cheap"))
    synthesis = _spans(result, "synthesis")[0]
    assert synthesis["start_ms"] >= max(_ends(_spans(result, "judge"))) - 5
    prompt = next(r for r in spy.requests if r.metadata.get("role") == "synthesizer").user_prompt
    assert '"judge_scores"' in prompt and "mock-fast" in prompt.split('"judge_scores"')[1]


def test_judge_feeds_synthesis_needs_a_judge_and_an_llm_aggregator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CHEAP__JUDGE_FEEDS_SYNTHESIS", "true")
    with pytest.raises(ConfigError, match="needs judge: light or full"):
        load_strategy_book()
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-DIGEST__JUDGE", "light")
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-DIGEST__JUDGE_FEEDS_SYNTHESIS", "true")
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CHEAP__JUDGE", "light")
    with pytest.raises(ConfigError, match="needs an llm aggregator"):
        load_strategy_book()


async def test_a_failing_stage_cancels_its_concurrent_sibling_and_raises_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _judge(monkeypatch)

    class BrokenSynth(Scripted):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("role") == "synthesizer":
                return ModelResponse(
                    provider="mock", model=request.model_id, error="down", error_type="Server"
                )
            return await super().complete(request)

    started = time.perf_counter()
    with pytest.raises(ProviderError, match="down"):  # not wrapped in an ExceptionGroup
        await _pipe(tmp_path, BrokenSynth({"judge": 5.0})).run(_ctx(strategy="panel-cheap"))
    assert time.perf_counter() - started < 2.0  # did not wait for the 5s judges
    await asyncio.sleep(0)
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


# ---------------------------------------------------------------------------------- shadow


async def test_the_shadow_baseline_runs_alongside_the_panel_not_after_it(tmp_path: Path) -> None:
    delays = {"mock-fast": 0.2, "mock-security": 0.2, "mock-weak": 0.2, "shadow_baseline": 0.5}
    pipe = _pipe(tmp_path, Scripted(delays))
    began = time.perf_counter()
    result = await pipe.run(_ctx(strategy="panel-cheap", shadow_baseline=True))
    elapsed = (time.perf_counter() - began) * 1000
    baseline = next(r for r in result.ledger.records if r.stage == "shadow_baseline")
    panel = _spans(result, "panel")
    assert baseline.started_at_ms < 100 and baseline.started_at_ms <= min(_starts(panel)) + 90
    assert baseline.started_at_ms < min(_ends(panel))  # overlaps the panel
    assert result.shadow is not None and result.shadow.ran
    # Fusion's own time stops when its answer is ready; the shadow is collected after that.
    assert result.total_latency_ms < 420
    assert result.shadow.fusion_latency_ms == pytest.approx(result.total_latency_ms)
    assert result.shadow.baseline_latency_ms is not None
    assert result.shadow.baseline_latency_ms >= 480
    assert elapsed < 650  # about the baseline's 500ms, not 500 + the 200ms panel and more
    assert result.total_cost_usd == result.ledger.total_cost().usd  # shadow cost is not Fusion's


async def test_shadow_calls_are_not_part_of_the_critical_path(tmp_path: Path) -> None:
    delays = {"shadow_baseline": 0.2, "mock-fast": 0.02, "mock-security": 0.02, "mock-weak": 0.02}
    result = await _pipe(tmp_path, Scripted(delays)).run(_ctx(shadow_baseline=True))
    metrics = result.task_metrics
    assert metrics is not None and metrics.critical_path_ms < 150
    assert metrics.calls == len([r for r in result.ledger.records if "shadow" not in r.stage])


async def test_no_shadow_call_starts_for_a_run_that_halts_before_the_panel(
    tmp_path: Path,
) -> None:
    spy = Scripted(0.0)
    result = await _pipe(tmp_path, spy).run(
        PipelineContext(task_type=TaskType.CODE_REVIEW, primary_content="x", shadow_baseline=True)
    )
    assert result.final_answer == "Insufficient context for analysis."
    assert not [r for r in spy.requests if r.metadata.get("role") == "shadow_baseline"]


async def test_a_run_that_halts_after_the_panel_cancels_the_shadow_call(tmp_path: Path) -> None:
    class Down(Scripted):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("role") == "panel":
                return ModelResponse(
                    provider="mock", model=request.model_id, error="down", error_type="Server"
                )
            return await super().complete(request)

    started = time.perf_counter()
    result = await _pipe(tmp_path, Down({"shadow_baseline": 5.0})).run(
        _ctx(strategy="panel-cheap", shadow_baseline=True)
    )
    assert "quorum was not met" in result.final_answer
    assert time.perf_counter() - started < 2.0
    assert result.shadow is None
    await asyncio.sleep(0)
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


async def test_a_failed_run_does_not_leave_the_shadow_call_running(tmp_path: Path) -> None:
    class BrokenSynth(Scripted):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("role") == "synthesizer":
                return ModelResponse(
                    provider="mock", model=request.model_id, error="down", error_type="Server"
                )
            return await super().complete(request)

    with pytest.raises(ProviderError):
        await _pipe(tmp_path, BrokenSynth({"shadow_baseline": 5.0})).run(
            _ctx(strategy="panel-cheap", shadow_baseline=True)
        )
    await asyncio.sleep(0)
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


# ------------------------------------------------------------------------------ refinement


async def test_refinement_starts_after_every_round_one_answer_and_all_at_once(
    tmp_path: Path,
) -> None:
    delays = {"mock-fast": 0.05, "mock-security": 0.1, "mock-weak": 0.15}

    class Refining(Scripted):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("role") == "refine":
                await asyncio.sleep(0.15)
            return await super().complete(request)

    result = await _pipe(tmp_path, Refining(delays)).run(_ctx(strategy="panel-refine"))
    panel, refine = _spans(result, "panel"), _spans(result, "refine")
    assert len(refine) == 3
    assert min(_starts(refine)) >= max(_ends(panel)) - 5  # the barrier: all peers answered
    assert max(_starts(refine)) - min(_starts(refine)) < 100  # then every revision starts together


class Agreeing(Scripted):
    """Every panelist raises the same claims, so the panel agrees completely."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("role") == "panel":
            claims = [
                {"text": "Missing error handling around the call", "kind": "finding"},
                {"text": "Add a regression test for the call", "kind": "test"},
            ]
            body = json.dumps({"summary": "same", "claims": claims, "confidence": 0.9})
            return ModelResponse(provider="mock", model=request.model_id, text=body)
        return await super().complete(request)


async def test_refinement_is_skipped_when_the_panel_already_agrees(tmp_path: Path) -> None:
    result = await _pipe(tmp_path, Agreeing(0.0)).run(_ctx(strategy="panel-refine"))
    assert result.agreement is not None and result.agreement.score == pytest.approx(1.0)
    assert not [r for r in result.ledger.records if r.stage == "refine"]
    assert any(
        "Refinement skipped: panel agreement 1.00 is at least 0.80" in w for w in result.warnings
    )
    assert result.refinement is not None and not result.refinement.ran


async def test_the_skip_can_be_turned_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION__REFINEMENT__SKIP_ABOVE_AGREEMENT", "null")
    result = await _pipe(tmp_path, Agreeing(0.0)).run(_ctx(strategy="panel-refine"))
    assert len([r for r in result.ledger.records if r.stage == "refine"]) == 3


async def test_the_skip_threshold_is_configurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = await _pipe(tmp_path, Scripted(0.0)).run(_ctx(strategy="panel-refine"))
    assert len([r for r in result.ledger.records if r.stage == "refine"]) == 3  # mock panel differs
    monkeypatch.setenv("FUSION__REFINEMENT__SKIP_ABOVE_AGREEMENT", "0.0")
    skipped = await _pipe(tmp_path, Scripted(0.0)).run(_ctx(strategy="panel-refine"))
    assert not [r for r in skipped.ledger.records if r.stage == "refine"]


# ---------------------------------------------------------------------------------- timeline


async def test_every_call_records_when_it_started_on_the_runs_own_clock() -> None:
    """Start offsets come from the ledger's injected clock, so a timeline is exact in tests."""
    now = [100.0]
    ledger = RunLedger(clock=lambda: now[0])
    catalog = load_catalog()

    class Ticking(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            response = await super().complete(request)
            now[0] += 0.25
            return response.model_copy(update={"latency_ms": 100.0})

    gateway = CallGateway(
        ledger=ledger,
        models=catalog.models,
        providers={"mock": Ticking(latency_ms=0.0)},
        pricing=PricingRegistry(),
    )
    request = ModelRequest(model_id="mock-fast", user_prompt="x")
    await gateway.call(stage="panel", alias="mock-fast", request=request)
    await gateway.call(stage="synthesis", alias="mock-judge", request=request)
    assert ledger.timeline() == [
        {
            "stage": "panel",
            "model": "mock-fast",
            "start_ms": 0.0,
            "end_ms": 100.0,
            "status": "success",
        },
        {
            "stage": "synthesis",
            "model": "mock-judge",
            "start_ms": 250.0,
            "end_ms": 350.0,
            "status": "success",
        },
    ]


async def test_critical_path_is_the_end_of_the_last_fusion_call(tmp_path: Path) -> None:
    delays = {"mock-fast": 0.05, "mock-security": 0.1, "mock-weak": 0.1, "synthesizer": 0.05}
    result = await _pipe(tmp_path, Scripted(delays)).run(_ctx(strategy="panel-cheap"))
    spans = [s for s in result.ledger.timeline() if "shadow" not in str(s["stage"])]
    metrics = result.task_metrics
    assert metrics is not None
    assert metrics.critical_path_ms == pytest.approx(max(_ends(spans)), abs=1.0)
    # The calls overlap, which is why the path is shorter than their sum. Checked on the calls' own
    # timestamps: comparing against the run's wall clock fails when a busy runner stalls the loop
    # between stages (a 250 ms gap was seen in CI).
    panel = [s for s in spans if s["stage"] == "panel"]
    assert max(_starts(panel)) < min(_ends(panel))  # every panel call began before any finished
