"""Latency: early return timed from measured speed, same-tier fallback, skipping the aggregator,
prompt caching, and the default strategy against one frontier model on the latency suite.

Fan-out timing runs on the virtual-time loop (``run_virtual``), so a scripted 30-second straggler
costs no real time and every timing below is exact.
"""

from __future__ import annotations

import asyncio
import statistics
from pathlib import Path
from typing import Any

import pytest

from _bench import sim_env
from _scripted import (
    AGREED,
    FAST,
    HIGH_RISK_PROMPT,
    SECURITY,
    UNRELATED,
    WEAK,
    Scripted,
    context,
    pipeline,
    stages,
)
from fusion.bench.arms import parse_arms
from fusion.bench.runner import run_bench
from fusion.bench.spec import BenchConfig
from fusion.bench.suite import load_suite
from fusion.bench.virtual import run_virtual
from fusion.config.catalog import ModelEntry, load_catalog, same_tier_spares, spare_tier
from fusion.config.loader import EarlyReturn, FanoutConfig, load_routing_policies
from fusion.orchestration.cascade import cascade_order
from fusion.orchestration.fanout import FanoutResult, fanout_to_panel
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.orchestration.prompts import compose_user_prompt
from fusion.orchestration.strategy import FanoutOverride, Strategy, load_strategy_book
from fusion.providers.base import (
    BadRequestError,
    ModelNotFoundError,
    ModelRequest,
    ModelResponse,
)
from fusion.providers.http_utils import classify_status
from fusion.providers.mock import MockProvider
from fusion.providers.simulated import SimModel, SimulatedProvider, SimWorld
from fusion.routing.budget import BudgetLevel
from fusion.routing.classifier import TaskType
from fusion.routing.model_registry import ModelRegistry
from fusion.routing.policy import RoutingPolicy
from fusion.telemetry.cost import PricingRegistry

TASK = "Retries around a flaky HTTP call keep timing out in production under load."


class Timed(MockProvider):
    """Mock answers after a scripted delay; a model in ``raises`` fails with that error."""

    def __init__(
        self, delays: dict[str, float], raises: dict[str, Exception] | None = None
    ) -> None:
        super().__init__(latency_ms=0.0)
        self.delays = delays
        self.raises = raises or {}
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        await asyncio.sleep(self.delays.get(request.model_id, 0.0))
        if request.model_id in self.raises:
            raise self.raises[request.model_id]
        return await super().complete(request)


def _models(**speeds: tuple[float, float] | None) -> dict[str, ModelEntry]:
    """The mock panel models, each with a measured (ttft ms, decode tok/s) or none."""
    catalog = load_catalog().models
    models: dict[str, ModelEntry] = {}
    for alias in (FAST, SECURITY, WEAK):
        speed = speeds.get(alias.replace("-", "_"))
        update: dict[str, Any] = {}
        if speed is not None:
            update = {"observed_ttft_ms": speed[0], "observed_decode_tps": speed[1]}
        models[alias] = catalog[alias].model_copy(update=update)
    return models


def _fan(
    provider: MockProvider,
    models: dict[str, ModelEntry],
    config: FanoutConfig,
    **kw: Any,
) -> tuple[FanoutResult, float]:
    """Run one fan-out on virtual time; returns the result and its wall time in seconds."""

    async def go() -> tuple[FanoutResult, float]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await fanout_to_panel(
            panel_models=kw.pop("panel", list(models)),
            registry_models=models,
            providers={"mock": provider},
            task_type=TaskType.DEBUGGING,
            primary_content=TASK,
            config=config,
            **kw,
        )
        return result, loop.time() - started

    return run_virtual(go())


EARLY = FanoutConfig(early_return=EarlyReturn(quorum=2, grace_ms="auto", max_grace_ms=1000))


# ------------------------------------------------------------------------------- early return


def test_one_slow_member_costs_quorum_plus_grace_not_the_stragglers_time() -> None:
    delays = {FAST: 2.0, SECURITY: 3.0, WEAK: 30.0}
    fixed = FanoutConfig(early_return=EarlyReturn(quorum=2, grace_ms=500))
    result, wall = _fan(Timed(delays), _models(), fixed)
    assert wall == pytest.approx(3.0 + 0.5, abs=0.05)  # quorum at 3 s, then the 500 ms grace
    assert result.early_return and result.success_count == 2 and result.grace_ms == 500
    straggler = next(c for c in result.calls if c.model_name == WEAK)
    assert straggler.status == "cancelled"


def test_auto_grace_waits_for_a_straggler_due_within_the_cap() -> None:
    # mock-weak is measured at 3.6 s to its first token and writes fast: due 0.6 s after quorum.
    models = _models(mock_weak=(3600.0, 1_000_000.0))
    result, wall = _fan(Timed({FAST: 2.0, SECURITY: 3.0, WEAK: 3.5}), models, EARLY)
    assert result.success_count == 3 and not result.early_return
    assert 0 < (result.grace_ms or 0) <= 1000
    assert wall == pytest.approx(3.5, abs=0.05)  # it came in during the grace


@pytest.mark.parametrize(
    "speed",
    [(9000.0, 1_000_000.0), (1000.0, 1_000_000.0)],
    ids=["due-after-the-cap", "already-overdue"],
)
def test_auto_grace_does_not_wait_for_a_straggler_that_will_not_make_it(
    speed: tuple[float, float],
) -> None:
    models = _models(mock_weak=speed)
    result, wall = _fan(Timed({FAST: 2.0, SECURITY: 3.0, WEAK: 30.0}), models, EARLY)
    assert result.early_return and result.grace_ms == 0
    assert wall == pytest.approx(3.0, abs=0.05)


def test_auto_grace_without_measured_speed_waits_the_cap() -> None:
    result, wall = _fan(Timed({FAST: 2.0, SECURITY: 3.0, WEAK: 30.0}), _models(), EARLY)
    assert result.grace_ms == 1000 and wall == pytest.approx(4.0, abs=0.05)


def test_auto_grace_expects_the_answered_members_output_length() -> None:
    # 1 ms to the first token, 10 tokens/s: the straggler's expected time is set by how much the
    # members who answered wrote. Their mock answers are long, so it is due long after the cap.
    models = _models(mock_weak=(1.0, 10.0))
    result, _ = _fan(Timed({FAST: 2.0, SECURITY: 3.0, WEAK: 30.0}), models, EARLY)
    assert result.grace_ms == 0


def test_the_shipped_policy_turns_early_return_and_hedging_on() -> None:
    fanout = load_routing_policies().fanout
    assert fanout.early_return is not None and fanout.early_return.grace_ms == "auto"
    assert fanout.hedge_after_ms is not None and fanout.prompt_cache == "auto"


def test_a_strategy_or_arm_can_switch_a_latency_control_off() -> None:
    base = load_routing_policies().fanout
    off = FanoutOverride.model_validate({"early_return": False, "hedge_after_ms": "off"})
    merged = off.applied_to(base)
    assert merged.early_return is None and merged.hedge_after_ms is None
    assert base.early_return is not None  # the shared policy is not changed
    assert FanoutConfig.model_validate({"early_return": False}).early_return is None
    with pytest.raises(ValueError, match="hedge_after_ms"):
        FanoutOverride.model_validate({"hedge_after_ms": 0})  # 0 is not a way to say "off"


# ---------------------------------------------------------------------- same-tier fallback


@pytest.mark.parametrize("status", [404, 410])
def test_a_missing_or_retired_model_is_its_own_error(status: int) -> None:
    error = classify_status(status, "model not found")
    assert isinstance(error, ModelNotFoundError) and error.error_type == "NotFound"


def test_a_failed_member_is_replaced_by_a_same_tier_model_and_quorum_holds() -> None:
    gone = ModelNotFoundError("anthropic API error 404: model retired")
    provider = Timed({FAST: 1.0, SECURITY: 2.0, WEAK: 1.5}, raises={FAST: gone})
    models = _models()
    result, wall = _fan(provider, models, FanoutConfig(), panel=[FAST, SECURITY])
    assert result.quorum_met and result.success_count == 2
    assert result.fallbacks == {FAST: WEAK}
    assert {c.model_name for c in result.calls} == {WEAK, SECURITY}  # who really answered
    assert any(f"{FAST} failed; asked {WEAK} (same tier: free)" in w for w in result.warnings)
    assert wall == pytest.approx(2.5, abs=0.05)  # the stand-in started when the 404 came back


def test_a_bad_request_is_not_retried_on_another_model() -> None:
    provider = Timed({}, raises={FAST: BadRequestError("prompt too long")})
    result, _ = _fan(provider, _models(), FanoutConfig(), panel=[FAST, SECURITY])
    assert result.fallbacks == {} and not result.quorum_met


def test_a_fallback_never_asks_another_member_of_the_strategy() -> None:
    provider = Timed({}, raises={FAST: ModelNotFoundError("gone")})
    result, _ = _fan(provider, _models(), FanoutConfig(), panel=[FAST, SECURITY], avoid=[WEAK])
    assert result.fallbacks == {FAST: ""}
    assert any("no other free-tier panel model was available" in w for w in result.warnings)


async def test_panel_duo_survives_a_retired_model(tmp_path: Path) -> None:
    provider = Scripted(fail=set())
    original = provider.complete

    async def retired(request: ModelRequest) -> ModelResponse:
        if request.model_id == FAST:
            raise ModelNotFoundError("anthropic API error 404: model not found")
        return await original(request)

    provider.complete = retired  # type: ignore[method-assign]
    result = await pipeline(tmp_path, provider).run(context(strategy="panel-duo"))
    assert result.halt_reason is None and result.final_answer
    assert result.fanout.fallbacks == {FAST: WEAK}
    assert {p.model_name for p in result.panel_results} == {WEAK, SECURITY}


def test_spares_share_the_failed_models_price_tier_and_prefer_another_vendor() -> None:
    models = load_catalog().models
    spares = same_tier_spares(
        models, "claude-haiku", output_tokens=1500, exclude=["gpt-luna"], usable=None
    )
    tier = spare_tier(models["claude-haiku"])
    assert spares and all(spare_tier(models[a]) == tier for a in spares)
    assert "gpt-luna-security" not in spares  # the same model as gpt-luna under another name
    assert all(models[a].enabled and "panel" in models[a].roles for a in spares)
    assert models[spares[0]].provider != "anthropic"
    luna_first = same_tier_spares(models, "claude-haiku", output_tokens=1500)
    assert luna_first[0] == "gpt-luna"  # measured speed ranks it ahead of unmeasured models


# ------------------------------------------------------------------------- skip the aggregator


async def test_panel_duo_skips_synthesis_when_the_panel_agrees(tmp_path: Path) -> None:
    provider = Scripted({FAST: AGREED, SECURITY: AGREED})
    result = await pipeline(tmp_path, provider).run(context(strategy="panel-duo"))
    assert "synthesis" not in stages(result)
    assert any(r.startswith("Synthesis skipped") for r in result.routing.reasons)
    assert result.final_answer


@pytest.mark.parametrize(
    ("answers", "prompt", "why"),
    [
        ({FAST: AGREED, SECURITY: UNRELATED}, None, "below the 0.60 threshold"),
        ({FAST: AGREED, SECURITY: AGREED}, HIGH_RISK_PROMPT, "high risk"),
    ],
    ids=["disagree", "high-risk"],
)
async def test_panel_duo_still_synthesizes_disagreement_and_high_risk(
    tmp_path: Path, answers: dict[str, str], prompt: str | None, why: str
) -> None:
    provider = Scripted(answers)
    ctx = context(prompt, strategy="panel-duo") if prompt else context(strategy="panel-duo")
    result = await pipeline(tmp_path, provider).run(ctx)
    assert "synthesis" in stages(result)
    assert any(r.startswith("Synthesis ran") and why in r for r in result.routing.reasons)


def test_skip_aggregator_needs_an_llm_aggregator() -> None:
    with pytest.raises(ValueError, match="skip_aggregator needs an llm aggregator"):
        Strategy.model_validate(
            {
                "name": "x",
                "kind": "panel",
                "members": [{"model": "claude-haiku"}, {"model": "gpt-luna"}],
                "aggregator": "vote",
                "skip_aggregator": {"agreement_threshold": 0.6},
            }
        )


async def test_the_aggregator_call_is_streamed(tmp_path: Path) -> None:
    provider = Scripted({FAST: AGREED, SECURITY: UNRELATED})
    await pipeline(tmp_path, provider).run(context(strategy="panel-duo"))
    synth = [r for r in provider.requests if r.metadata.get("role") == "synthesizer"]
    assert synth and all(r.stream for r in synth)


# ------------------------------------------------------------------------- effort and speed


def test_the_defaults_panel_runs_at_low_reasoning_effort() -> None:
    catalog = load_catalog().models
    default = load_strategy_book().for_budget(BudgetLevel.MEDIUM)
    for member in default.members:
        entry = catalog[member.model]
        effort = member.reasoning_effort or entry.default_reasoning_effort
        assert entry.supports_reasoning_effort and effort in {"none", "minimal", "low"}, member


def test_speed_fast_ranks_members_by_measured_speed() -> None:
    book = load_strategy_book()
    nano = book.get("panel-nano")
    fast = nano.model_copy(update={"speed": "fast"})
    models, pricing = load_catalog().models, PricingRegistry()
    # The 0.3.0 study's speeds, over a 1,500-token answer: Haiku 1.1 s to the first token at
    # 245 tok/s (7.2 s), Luna 3.9 s at 179 tok/s (12.3 s), Gemini Flash 2.8 s at 145 tok/s (13.2 s).
    order = ["claude-haiku", "gpt-luna", "gemini-flash"]
    assert [m.model for m in cascade_order(fast, fast.members, models, pricing)] == order
    cheapest = [m.model for m in cascade_order(nano, nano.members, models, pricing)]
    assert cheapest[-1] == "gemini-flash"  # balanced: a cascade still goes cheapest first
    router = RoutingPolicy(
        load_routing_policies(), registry=ModelRegistry.for_mode(use_mock=False), strategies=book
    ).router
    decision = router.route(strategy=fast, explicit_type="code_review", content=TASK)
    assert decision.selected_panel == order


def test_measured_speed_gives_an_expected_answer_time() -> None:
    haiku = load_catalog().models["claude-haiku"]
    assert haiku.expected_answer_ms(2450) == pytest.approx(1106 + 10_000)
    assert load_catalog().models["deepseek-flash"].expected_answer_ms(1000) is None


# --------------------------------------------------------------------------- prompt caching


def test_calls_about_the_same_files_share_their_prompt_prefix() -> None:
    files = ["src/app.py\nimport os\n" * 50]
    first = compose_user_prompt(
        task_type=TaskType.DEFAULT, primary_content="Why?", file_snippets=files
    )
    second = compose_user_prompt(
        task_type=TaskType.DEFAULT, primary_content="And how to fix it?", file_snippets=files
    )
    assert first.shared_prefix == second.shared_prefix > len(files[0])
    assert first.text[: first.shared_prefix] == second.text[: second.shared_prefix]
    assert "Why?" not in first.text[: first.shared_prefix]  # the request comes after the files
    bare = compose_user_prompt(task_type=TaskType.DEFAULT, primary_content="Why?")
    assert bare.shared_prefix == 0


def test_panel_calls_mark_the_shared_prefix_for_the_prompt_cache() -> None:
    provider = Timed({})
    _fan(provider, _models(), FanoutConfig(), file_snippets=["a.py\nx = 1\n"])
    prefixes = {r.user_prompt[: r.shared_prefix_chars or 0] for r in provider.requests}
    assert len(prefixes) == 1 and all(r.cache_prefix for r in provider.requests)
    assert len({r.system_prompt for r in provider.requests}) == 1  # one system prompt for all
    security = next(r for r in provider.requests if r.model_id == SECURITY)  # has a persona
    assert security.user_prompt.index("## Your Role") > (security.shared_prefix_chars or 0)
    plain = Timed({})
    _fan(plain, _models(), FanoutConfig())  # no files: nothing worth a cache write
    assert not any(r.cache_prefix for r in plain.requests)
    off = Timed({})
    _fan(off, _models(), FanoutConfig(prompt_cache="off"), file_snippets=["a.py"])
    assert not any(r.cache_prefix for r in off.requests)


async def test_cached_input_tokens_are_priced_at_the_cached_rate() -> None:
    class Cached(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(
                provider="anthropic",
                model=request.model_id,
                text="ok",
                input_tokens=10_000,
                cached_input_tokens=8_000,
                output_tokens=1_000,
            )

    catalog = load_catalog()
    ledger = RunLedger()
    gateway = CallGateway(
        ledger=ledger,
        models=catalog.models,
        providers={"anthropic": Cached()},
        pricing=PricingRegistry(catalog),
    )
    request = ModelRequest(model_id="claude-haiku-5-5", user_prompt="x")
    await gateway.call(stage="panel", alias="claude-haiku", request=request)
    record = ledger.records[0]
    # Haiku 5.5: $0.10 input, $0.01 cached input, $0.50 output per 1M tokens.
    expected = (2_000 * 0.10 + 8_000 * 0.01 + 1_000 * 0.50) / 1_000_000
    assert record.cached_tokens == 8_000 and record.cost_usd == pytest.approx(expected)
    assert ledger.by_stage()["panel"].cached_tokens == 8_000
    assert ledger.task_metrics(1000.0).cached_input_tokens == 8_000


def test_a_simulated_prompt_cache_serves_a_later_call_not_a_concurrent_one() -> None:
    spec = SimModel(input_per_1m=1.0, output_per_1m=1.0, cached_input_per_1m=0.1, ttft_ms=100)
    sim = SimulatedProvider("anthropic", {"m": spec}, SimWorld([]), prompt_cache="explicit")
    prefix = "## Task\n" + "file contents " * 200

    def request(question: str) -> ModelRequest:
        return ModelRequest(
            model_id="m",
            user_prompt=prefix + question,
            cache_prefix=True,
            shared_prefix_chars=len(prefix),
        )

    async def go() -> list[ModelResponse]:
        together = await asyncio.gather(sim.complete(request("a?")), sim.complete(request("b?")))
        later = await sim.complete(request("c?"))
        return [*together, later]

    first, second, later = run_virtual(go())
    assert not first.cached_input_tokens and not second.cached_input_tokens  # in flight together
    assert later.cached_input_tokens and later.cached_input_tokens >= len(prefix) // 3
    assert (later.actual_cost_usd or 0) < (first.actual_cost_usd or 0)
    off = SimulatedProvider("anthropic", {"m": spec}, SimWorld([]))  # studies: off
    assert not run_virtual(off.complete(request("d?"))).cached_input_tokens


# ------------------------------------------------------------------------ the done-when check


def test_the_defaults_simulated_median_is_no_slower_than_one_frontier_model(
    tmp_path: Path,
) -> None:
    """Roadmap Task 6: on the latency suite, simulated with the catalog's measured speeds, the
    default strategy's median time per task is at most Opus 5.5's. The live check is Task 16's."""
    data = dict(load_suite("latency").config)
    default = load_strategy_book().for_budget(BudgetLevel.MEDIUM).name
    data.update(
        dataset="evals/datasets/v1",
        arms=parse_arms(f"solo-opus,{default}"),
        mock=True,
        max_usd=100.0,
        repeats=2,
    )
    data.pop("spend_stop_usd", None)
    cfg = BenchConfig.model_validate(data)
    run = run_virtual(run_bench(cfg, env=sim_env(cfg, tmp_path), run_id="latency"))
    seconds: dict[str, list[float]] = {}
    for item in run.items:
        seconds.setdefault(item.arm, []).append(item.metrics.seconds_to_complete)
    assert len(seconds["solo-opus"]) == len(seconds[default]) == 18  # 9 tasks x 2 repeats
    assert statistics.median(seconds[default]) <= statistics.median(seconds["solo-opus"])
