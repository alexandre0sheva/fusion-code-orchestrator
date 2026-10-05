"""RunLedger totals and CallGateway accounting: every LLM call is recorded exactly once."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from fusion.config.catalog import Catalog, ModelEntry, PriceSchedule
from fusion.orchestration.ledger import CallGateway, CallRecord, RunLedger
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.telemetry.cost import PricingRegistry


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def rec(stage: str = "panel", cost: float | None = 0.01, **kw: object) -> CallRecord:
    fields: dict[str, object] = {
        "stage": stage,
        "model_alias": "m",
        "provider": "p",
        "model_id": "m-1",
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": cost,
        "cost_known": cost is not None,
        "latency_ms": 1000.0,
        "started_at_ms": 0.0,
        "ok": True,
    }
    fields.update(kw)
    return CallRecord(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------- ledger


def test_total_cost_is_the_sum_of_arm_calls_including_judge_and_eval() -> None:
    ledger = RunLedger()
    for stage, cost in [("panel", 0.01), ("refine", 0.02), ("judge", 0.03), ("synthesis", 0.04)]:
        ledger.add(rec(stage, cost))
    ledger.add(rec("eval", 0.05))
    total = ledger.total_cost()
    assert total.usd == pytest.approx(0.15)
    assert total.known and total.calls == 5


def test_shadow_calls_are_excluded_from_fusion_cost_by_default() -> None:
    ledger = RunLedger()
    ledger.add(rec("panel", 0.01))
    ledger.add(rec("shadow_baseline", 1.0))
    ledger.add(rec("shadow_judge", 0.5))
    assert ledger.total_cost().usd == pytest.approx(0.01)
    everything = ledger.total_cost(exclude_stages=set())
    assert everything.usd == pytest.approx(1.51)


def test_include_stages_restricts_the_sum() -> None:
    ledger = RunLedger()
    ledger.add(rec("panel", 0.01))
    ledger.add(rec("judge", 0.03))
    assert ledger.total_cost(include_stages={"judge"}).usd == pytest.approx(0.03)


def test_unknown_cost_makes_the_total_unknown_but_keeps_the_known_part() -> None:
    ledger = RunLedger()
    ledger.add(rec("panel", 0.01))
    ledger.add(rec("panel", None, ok=False))
    total = ledger.total_cost()
    assert total.usd == pytest.approx(0.01)
    assert total.known is False


def test_by_stage_groups_calls_tokens_and_cost() -> None:
    ledger = RunLedger()
    ledger.add(rec("panel", 0.01))
    ledger.add(rec("panel", 0.02, ok=False))
    ledger.add(rec("judge", 0.03))
    stages = ledger.by_stage()
    assert stages["panel"].calls == 2 and stages["panel"].ok_calls == 1
    assert stages["panel"].cost_usd == pytest.approx(0.03)
    assert stages["panel"].input_tokens == 200
    assert stages["judge"].calls == 1


def test_task_metrics_report_wall_time_tokens_speed_and_critical_path() -> None:
    ledger = RunLedger()
    ledger.add(rec("panel", 0.01, started_at_ms=0, latency_ms=1000, retries=1, reasoning_tokens=10))
    ledger.add(rec("panel", 0.01, started_at_ms=0, latency_ms=3000, output_tokens=150))
    ledger.add(rec("synthesis", 0.02, started_at_ms=3100, latency_ms=900))
    ledger.add(rec("shadow_baseline", 9.0, started_at_ms=0, latency_ms=20000))
    metrics = ledger.task_metrics(wall_ms=4000.0)
    assert metrics.seconds_to_complete == pytest.approx(4.0)
    assert metrics.cost_usd == pytest.approx(0.04)
    assert metrics.input_tokens == 300
    assert metrics.output_tokens == 250
    assert metrics.reasoning_tokens == 10
    assert metrics.calls == 3 and metrics.retries == 1
    assert metrics.effective_output_tokens_per_s == pytest.approx(250 / 4.0)
    assert metrics.critical_path_ms == pytest.approx(4000.0)  # synthesis ends at 3100 + 900


# --------------------------------------------------------------------------------------- gateway


def _catalog() -> Catalog:
    price = PriceSchedule(
        input_per_1m=1.0,
        output_per_1m=2.0,
        verified_on=date(2026, 10, 5),
        source_url="https://example.test/p",
    )
    models = {
        "small": ModelEntry(
            alias="small",
            provider="fake",
            model_id="small-1",
            prices=[price],
            context_window=2000,
            max_tokens=100,
        ),
        "big": ModelEntry(alias="big", provider="fake", model_id="big-1", prices=[price]),
        "orphan": ModelEntry(alias="orphan", provider="ghost", model_id="o-1", prices=[price]),
    }
    return Catalog(models=models)


class ScriptedProvider(ModelProvider):
    name = "fake"

    def __init__(self, clock: Clock, *, delay: float = 0.0, error: str | None = None) -> None:
        self.clock = clock
        self.delay = delay
        self.error = error
        self.requests: list[ModelRequest] = []

    def is_available(self) -> bool:
        return True

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.clock.now += 2.0
        if self.error:
            return ModelResponse(
                provider="fake", model=request.model_id, error=self.error, error_type="Server"
            )
        return ModelResponse(
            provider="fake",
            model=request.model_id,
            text="ok",
            input_tokens=1000,
            output_tokens=500,
            cached_input_tokens=200,
            reasoning_tokens=40,
            latency_ms=2000.0,
            ttft_ms=300.0,
            total_tokens_per_s=250.0,
            decode_tokens_per_s=300.0,
            retries=2,
        )


def _gateway(provider: ModelProvider, clock: Clock) -> tuple[CallGateway, RunLedger, list[str]]:
    catalog = _catalog()
    ledger = RunLedger(clock=clock)
    warnings: list[str] = []
    gateway = CallGateway(
        ledger=ledger,
        models=catalog.models,
        providers={"fake": provider},
        pricing=PricingRegistry(catalog),
        warnings=warnings,
    )
    return gateway, ledger, warnings


async def test_gateway_records_priced_successful_call() -> None:
    clock = Clock()
    gateway, ledger, _ = _gateway(ScriptedProvider(clock), clock)
    clock.now += 0.5  # the run started half a second before the call
    response = await gateway.call(
        stage="panel", alias="big", request=ModelRequest(model_id="big-1", user_prompt="hi")
    )
    assert response.model_alias == "big"
    (record,) = ledger.records
    assert (record.stage, record.model_alias, record.provider) == ("panel", "big", "fake")
    assert (record.input_tokens, record.output_tokens) == (1000, 500)
    assert (record.cached_tokens, record.reasoning_tokens) == (200, 40)
    # 800 plain input + 200 cached (input rate, no cached price) + 500 output
    assert record.cost_usd == pytest.approx((1000 * 1.0 + 500 * 2.0) / 1_000_000)
    assert record.cost_known and record.ok
    assert record.started_at_ms == pytest.approx(500.0)
    assert record.ttft_ms == 300.0 and record.output_tokens_per_s == 250.0
    assert record.retries == 2


async def test_gateway_records_failures_with_unknown_cost() -> None:
    clock = Clock()
    gateway, ledger, _ = _gateway(ScriptedProvider(clock, error="boom"), clock)
    response = await gateway.call(
        stage="judge", alias="big", request=ModelRequest(model_id="big-1", user_prompt="hi")
    )
    assert not response.ok
    (record,) = ledger.records
    assert not record.ok and record.error == "boom" and record.error_type == "Server"
    assert record.cost_known is False


async def test_gateway_timeout_is_recorded_and_reported() -> None:
    clock = Clock()
    gateway, ledger, _ = _gateway(ScriptedProvider(clock, delay=0.2), clock)
    response = await gateway.call(
        stage="panel",
        alias="big",
        request=ModelRequest(model_id="big-1", user_prompt="hi"),
        timeout=0.01,
    )
    assert response.error_type == "TimeoutError"
    (record,) = ledger.records
    assert record.status == "timeout" and not record.ok and record.cost_known is False


async def test_gateway_missing_provider_costs_nothing() -> None:
    clock = Clock()
    gateway, ledger, _ = _gateway(ScriptedProvider(clock), clock)
    response = await gateway.call(
        stage="panel", alias="orphan", request=ModelRequest(model_id="o-1", user_prompt="hi")
    )
    assert response.error_type == "MissingProvider"
    (record,) = ledger.records
    assert record.status == "missing_provider" and record.cost_usd == 0.0 and record.cost_known


async def test_gateway_trims_oversized_prompts_and_says_so() -> None:
    clock = Clock()
    provider = ScriptedProvider(clock)
    gateway, ledger, warnings = _gateway(provider, clock)
    huge = "x" * 50_000  # far beyond a 2,000-token window
    await gateway.call(
        stage="panel",
        alias="small",
        request=ModelRequest(model_id="small-1", user_prompt=huge, max_tokens=100),
    )
    sent = provider.requests[0].user_prompt
    assert len(sent) < len(huge)
    assert "omitted to fit" in sent
    assert len(warnings) == 1 and "small" in warnings[0] and "trimmed" in warnings[0]
    assert ledger.records[0].trimmed is True


async def test_gateway_never_trims_prompts_that_fit() -> None:
    clock = Clock()
    provider = ScriptedProvider(clock)
    gateway, ledger, warnings = _gateway(provider, clock)
    prompt = "y" * 3000
    await gateway.call(
        stage="panel",
        alias="small",
        request=ModelRequest(model_id="small-1", user_prompt=prompt, max_tokens=100),
    )
    assert provider.requests[0].user_prompt == prompt
    assert warnings == [] and ledger.records[0].trimmed is False


async def test_models_without_a_context_window_are_never_trimmed() -> None:
    clock = Clock()
    provider = ScriptedProvider(clock)
    gateway, _, warnings = _gateway(provider, clock)
    prompt = "z" * 400_000
    await gateway.call(
        stage="panel", alias="big", request=ModelRequest(model_id="big-1", user_prompt=prompt)
    )
    assert provider.requests[0].user_prompt == prompt and warnings == []
