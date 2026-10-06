"""Strategies (declarative arms) and the two run modes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastmcp import Client
from pydantic import ValidationError
from typer.testing import CliRunner

from fusion.cli.app import app
from fusion.config.catalog import load_catalog
from fusion.config.layers import ConfigError
from fusion.config.loader import load_routing_policies
from fusion.mcp_server.server import create_mcp_server
from fusion.orchestration.claims import parse_panel_answer, render_panel_answer
from fusion.orchestration.context import PipelineContext, RunState
from fusion.orchestration.factory import Settings, build_deps, build_pipeline
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.orchestration.schemas import FusionAskInput
from fusion.orchestration.stages import RedactStage, RouteStage
from fusion.orchestration.strategy import (
    MODE_SETTINGS,
    Mode,
    PanelMember,
    Strategy,
    load_strategy_book,
    mock_strategy_book,
)
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.providers.mock import MockProvider
from fusion.routing.budget import BudgetLevel
from fusion.routing.classifier import TaskType
from fusion.routing.model_registry import ModelRegistry
from fusion.telemetry.cost import PricingRegistry

PROMPT = "How should I retry a failed HTTP call in httpx? Include a test strategy."


class SpyProvider(MockProvider):
    """Mock provider that remembers every request it served."""

    def __init__(self) -> None:
        super().__init__(latency_ms=0.0)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


def _pipeline(tmp_path: Path, provider: MockProvider | None = None, **kw: Any) -> Any:
    settings = Settings(use_mock=True, db_path=str(tmp_path / "s.db"), **kw)
    return build_pipeline(settings, {"mock": provider or MockProvider(latency_ms=0.0)})


def _ctx(**kw: Any) -> PipelineContext:
    return PipelineContext(task_type=TaskType.DEFAULT, primary_content=PROMPT, **kw)


def _stages(result: Any) -> list[str]:
    assert result.ledger is not None
    return [r.stage for r in result.ledger.records]


# ----------------------------------------------------------------------------------- validation


def _member(alias: str = "claude-haiku", **kw: Any) -> dict[str, Any]:
    return {"model": alias, **kw}


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"kind": "solo", "members": [_member(), _member("gpt-luna")]}, "exactly one member"),
        ({"kind": "solo", "members": [_member()], "rounds": 2}, "rounds: 1"),
        (
            {"kind": "solo", "members": [_member()], "aggregator_model": "claude-sonnet"},
            "no aggregator_model",
        ),
        ({"kind": "panel", "members": [_member(), _member()]}, "only once"),
        ({"kind": "panel", "members": [_member(), _member("gpt-luna")], "rounds": 0}, "rounds"),
        (
            {"kind": "panel", "members": [_member(), _member("gpt-luna")], "judge": "heavy"},
            "judge",
        ),
        (
            {
                "kind": "panel",
                "members": [_member(), _member("gpt-luna")],
                "aggregator": "digest",
                "aggregator_model": "claude-sonnet",
            },
            "calls no model",
        ),
        (
            {"kind": "cascade", "members": [_member(), _member("gpt-luna")]},
            "more than cascade.first",
        ),
        (
            {
                "kind": "cascade",
                "members": [_member(), _member("gpt-luna"), _member("gemini-flash")],
                "cascade": {"first": 1},
            },
            "greater than or equal to 2",
        ),
        (
            {
                "kind": "cascade",
                "members": [_member(), _member("gpt-luna"), _member("gemini-flash")],
                "cascade": {"early_aggregator": "llm"},
            },
            "early_aggregator",
        ),
        (
            {
                "kind": "panel",
                "members": [_member(), _member("gpt-luna")],
                "cascade": {"first": 2},
            },
            "only valid for kind 'cascade'",
        ),
        (
            {
                "kind": "panel",
                "members": [_member(), _member("gpt-luna")],
                "aggregator": "vote",
                "aggregator_model": "claude-sonnet",
            },
            "vote aggregator calls no model",
        ),
        ({"kind": "panel", "members": [_member(role="poet"), _member("gpt-luna")]}, "unknown role"),
        (
            {"kind": "panel", "members": [_member(temperature=3.0), _member("gpt-luna")]},
            "less than",
        ),
        ({"kind": "panel", "members": []}, "at least 1"),
        (
            {"kind": "panel", "members": [_member(), _member("gpt-luna")], "max_cost_usd": 0},
            "greater",
        ),
        ({"kind": "panel", "members": [_member(), _member("gpt-luna")], "colour": "red"}, "colour"),
    ],
)
def test_strategy_validation_rejects_inconsistent_specs(spec: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Strategy.model_validate({"name": "t", **spec})


def test_a_valid_panel_strategy_keeps_every_field() -> None:
    strategy = Strategy.model_validate(
        {
            "name": "t",
            "kind": "panel",
            "members": [
                _member(role="security_reviewer", temperature=0.2, reasoning_effort="high"),
                _member("gpt-luna"),
            ],
            "rounds": 3,
            "aggregator_model": "claude-sonnet",
            "judge": "light",
            "max_cost_usd": 0.05,
            "max_latency_s": 30,
        }
    )
    assert strategy.members[0] == PanelMember(
        model="claude-haiku", role="security_reviewer", reasoning_effort="high", temperature=0.2
    )
    assert strategy.models == ["claude-haiku", "gpt-luna", "claude-sonnet"]
    assert strategy.aggregator == "llm" and strategy.max_latency_s == 30


def test_packaged_strategies_are_the_documented_set() -> None:
    book = load_strategy_book()
    assert set(book.names()) == {
        "solo-frontier",
        "solo-sol",
        "solo-cheap",
        "solo-luna",
        "panel-duo",
        "panel-cheap",
        "panel-cheap-strong-synth",
        "panel-refine",
        "panel-digest",
        "panel-vote",
        "panel-cascade",
        "panel-local",
        "best-of-n-verified",
    }
    assert book.get("best-of-n-verified").aggregator == "verified"
    assert book.get("solo-frontier").members[0].model == "claude-opus"
    assert book.get("panel-cheap").aggregator_model == "claude-haiku"
    assert book.get("panel-cheap-strong-synth").aggregator_model == "claude-sonnet"
    assert book.get("panel-refine").rounds == 2
    assert book.get("panel-digest").aggregator == "digest"
    assert book.get("panel-vote").aggregator == "vote"
    cascade = book.get("panel-cascade")
    assert cascade.kind == "cascade" and cascade.cascade is not None
    assert (cascade.cascade.first, cascade.cascade.early_aggregator) == (2, "vote")
    assert all(s.judge == "off" for s in book.strategies.values())


def test_every_packaged_strategy_references_catalog_models() -> None:
    catalog = load_catalog()
    assert load_strategy_book().referenced_models() <= set(catalog.models)


def test_unknown_strategy_suggests_the_closest_name() -> None:
    with pytest.raises(ConfigError, match="did you mean 'panel-cheap'"):
        load_strategy_book().get("panel-cheep")


def test_budget_map_must_name_defined_strategies(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "strategies": {"a": {"kind": "solo", "members": [{"model": "claude-haiku"}]}},
                "budget_strategies": {"low": "nope"},
            }
        )
    )
    with pytest.raises(ConfigError, match="budget_strategies.low names unknown strategy 'nope'"):
        load_strategy_book(path)


# ------------------------------------------------------------------------------------- layering


def test_user_config_can_override_one_field_or_add_a_strategy(fusion_home: Path) -> None:
    (fusion_home / "config" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "strategies": {
                    "panel-cheap": {"aggregator_model": "claude-sonnet"},
                    "mine": {
                        "kind": "solo",
                        "members": [{"model": "gpt-sol", "reasoning_effort": "high"}],
                    },
                },
                "budget_strategies": {"low": "mine"},
            }
        )
    )
    book = load_strategy_book()
    assert book.get("panel-cheap").aggregator_model == "claude-sonnet"
    assert len(book.get("panel-cheap").members) == 3  # untouched fields keep their defaults
    assert book.get("mine").name == "mine"
    assert book.for_budget(BudgetLevel.LOW).name == "mine"
    assert book.for_budget(BudgetLevel.MEDIUM).name == "panel-duo"


def test_invalid_user_strategy_reports_key_value_and_layer(
    fusion_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CHEAP__ROUNDS", "9")
    with pytest.raises(ConfigError) as exc:
        load_strategy_book()
    text = str(exc.value)
    assert "strategies.panel-cheap.rounds = 9" in text
    assert "environment variable FUSION__STRATEGIES__PANEL-CHEAP__ROUNDS" in text


def test_removed_policy_and_refinement_keys_point_at_strategies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FUSION__POLICIES__DEBUGGING__PANEL_MODELS", "[claude-haiku]")
    with pytest.raises(ConfigError, match="panel_models .*strategies"):
        load_routing_policies()
    monkeypatch.delenv("FUSION__POLICIES__DEBUGGING__PANEL_MODELS")
    monkeypatch.setenv("FUSION__REFINEMENT__MAX_ROUNDS", "2")
    with pytest.raises(ConfigError, match="max_rounds .*rounds"):
        load_routing_policies()


# ----------------------------------------------------------------------------- offline (mock)


def test_mock_strategy_book_keeps_shapes_on_mock_models() -> None:
    registry = ModelRegistry.for_mode(use_mock=True)
    mocked = mock_strategy_book(load_strategy_book(), registry)
    assert [m.model for m in mocked.get("panel-cheap").members] == [
        "mock-fast",
        "mock-security",
        "mock-weak",
    ]
    assert [m.model for m in mocked.get("solo-frontier").members] == ["mock-fast"]
    assert mocked.get("panel-cheap").aggregator_model == "mock-judge"
    assert mocked.get("panel-digest").aggregator_model is None
    assert mocked.get("panel-refine").rounds == 2


# ------------------------------------------------------------------------------------ solo path


async def test_solo_is_one_provider_call_and_one_ledger_record(tmp_path: Path) -> None:
    spy = SpyProvider()
    result = await _pipeline(tmp_path, spy).run(_ctx(strategy="solo-frontier"))
    assert len(spy.requests) == 1
    assert _stages(result) == ["panel"]
    assert len(result.ledger.records) == 1
    assert result.routing.strategy == "solo-frontier"
    answer, valid = parse_panel_answer(result.panel_results[0].content)
    assert valid and result.final_answer == render_panel_answer(answer)  # the answer as is
    assert result.usage.successful_model_calls == 1
    assert result.disagreement["low_information"] is True  # one model has nobody to agree with
    assert result.disagreement["consensus"] is False


async def test_budget_levels_resolve_to_strategies_and_an_explicit_strategy_wins(
    tmp_path: Path,
) -> None:
    pipe = _pipeline(tmp_path)
    low = await pipe.run(_ctx(budget=BudgetLevel.LOW))
    medium = await pipe.run(_ctx())
    high = await pipe.run(_ctx(budget=BudgetLevel.HIGH))
    explicit = await pipe.run(_ctx(budget=BudgetLevel.HIGH, strategy="solo-luna"))
    assert [r.routing.strategy for r in (low, medium, high, explicit)] == [
        "solo-cheap",
        "panel-duo",
        "panel-refine",
        "solo-luna",
    ]
    assert _stages(low) == ["panel"]
    assert _stages(medium).count("panel") == 2 and "refine" not in _stages(medium)  # panel-duo
    assert _stages(high).count("refine") == 3
    assert _stages(explicit) == ["panel"]


async def test_unknown_strategy_fails_before_a_run_is_recorded(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path)
    with pytest.raises(ConfigError, match="Unknown strategy 'nope'"):
        await pipe.run(_ctx(strategy="nope"))
    assert pipe.deps.run_store.list_runs() == []


# ------------------------------------------------------------------------- aggregators, rounds


async def test_digest_makes_no_synthesis_call_and_returns_every_answer(tmp_path: Path) -> None:
    spy = SpyProvider()
    result = await _pipeline(tmp_path, spy).run(_ctx(strategy="panel-digest"))
    assert len(spy.requests) == 3
    assert "synthesis" not in _stages(result)
    assert result.routing.synthesizer_model == ""
    for panel in result.panel_results:
        answer, _ = parse_panel_answer(panel.content)
        assert f"### Answer from {panel.model_name}" in result.final_answer
        assert render_panel_answer(answer) in result.final_answer
    assert "### Shared by most models" in result.final_answer
    assert result.final_answer.startswith("## Panel digest: 3 answers")


async def test_llm_aggregator_makes_one_synthesis_call(tmp_path: Path) -> None:
    result = await _pipeline(tmp_path).run(_ctx(strategy="panel-cheap"))
    assert _stages(result).count("synthesis") == 1
    assert result.routing.synthesizer_model == "mock-judge"


async def test_rounds_set_the_number_of_refinement_rounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-REFINE__ROUNDS", "3")
    result = await _pipeline(tmp_path).run(_ctx(strategy="panel-refine"))
    assert _stages(result).count("refine") == 6  # two rounds x three members
    assert result.refinement is not None and len(result.refinement.calls) == 6


async def test_judge_level_controls_judge_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, three_model_default: None
) -> None:
    off = await _pipeline(tmp_path).run(_ctx())
    assert "judge" not in _stages(off)
    assert off.evals is not None and off.evals.judge_quality is None
    assert all("LLM judge off" in p.evaluation.judge_notes for p in off.panel_results)

    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CHEAP__JUDGE", "light")
    light = await _pipeline(tmp_path).run(_ctx())
    assert _stages(light).count("judge") == 3
    assert light.evals is not None and light.evals.judge_quality is None

    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CHEAP__JUDGE", "full")
    full = await _pipeline(tmp_path).run(_ctx())
    assert _stages(full).count("judge") == 3
    assert full.evals is not None and full.evals.judge_quality is not None


async def test_settings_can_still_turn_the_judge_off_globally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CHEAP__JUDGE", "full")
    result = await _pipeline(tmp_path, use_llm_judge=False).run(_ctx())
    assert "judge" not in _stages(result)


async def test_member_settings_reach_every_call_of_that_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = (
        "[{model: claude-haiku, role: security_reviewer, temperature: 0.25, "
        "reasoning_effort: high}, {model: gpt-luna}, {model: gemini-flash}]"
    )
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-REFINE__MEMBERS", members)
    spy = SpyProvider()
    await _pipeline(tmp_path, spy).run(_ctx(strategy="panel-refine"))
    first = [r for r in spy.requests if r.model_id == "mock-fast"]
    other = [r for r in spy.requests if r.model_id == "mock-security"]
    assert len(first) == 2  # panel answer and refinement
    assert all(r.temperature == 0.25 and r.reasoning_effort == "high" for r in first)
    assert all("security-focused" in r.system_prompt for r in first[:1])
    assert all(r.temperature is None and r.reasoning_effort is None for r in other)


async def test_strategy_caps_warn_when_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__STRATEGIES__SOLO-CHEAP__MAX_LATENCY_S", "0.000001")
    result = await _pipeline(tmp_path, MockProvider(latency_ms=20.0)).run(
        _ctx(strategy="solo-cheap")
    )
    assert any("Strategy 'solo-cheap' took" in w and "cap" in w for w in result.warnings)


# --------------------------------------------------------------------------------------- stages


async def test_route_stage_applies_the_strategy_to_the_state(tmp_path: Path) -> None:
    deps = build_deps(
        Settings(use_mock=True, db_path=str(tmp_path / "r.db")),
        {"mock": MockProvider(latency_ms=0.0)},
    )
    state = RunState.start(_ctx(strategy="solo-frontier"), deps)
    assert state.strategy is not None and state.strategy.name == "solo-frontier"
    state = await RedactStage(deps).run(state)
    state = await RouteStage(deps).run(state)
    assert state.panel_models == ["mock-fast"]
    assert [m.model for m in state.members] == ["mock-fast"]
    assert state.synthesizer_model == ""
    stored = deps.run_store.get_run(state.run_id)
    assert stored is not None
    deps.run_store.close()


# ----------------------------------------------------------------------------------- run modes


def test_mode_settings_differ_where_the_roadmap_says() -> None:
    real, bench = MODE_SETTINGS[Mode.REAL], MODE_SETTINGS[Mode.BENCHMARK]
    assert (real.lifetime_stats, real.shadow, real.truncate_prompts) == (True, True, True)
    assert (real.temperature, real.seed) == (None, None)
    assert (bench.lifetime_stats, bench.shadow, bench.truncate_prompts) == (False, False, False)
    assert (bench.temperature, bench.seed) == (0.0, 0)
    assert (real.redact, real.stream) == (True, False)
    assert (bench.redact, bench.stream) == (False, True)


async def test_benchmark_mode_fixes_temperature_and_seed(tmp_path: Path) -> None:
    spy = SpyProvider()
    pipe = _pipeline(tmp_path, spy)
    await pipe.run(_ctx(strategy="panel-cheap"), mode=Mode.BENCHMARK)
    assert spy.requests and all(r.temperature == 0.0 and r.seed == 0 for r in spy.requests)
    spy.requests.clear()
    await pipe.run(_ctx(strategy="panel-cheap"))
    assert all(r.temperature is None and r.seed is None for r in spy.requests)


async def test_a_member_temperature_beats_the_benchmark_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FUSION__STRATEGIES__SOLO-CHEAP__MEMBERS", "[{model: claude-haiku, temperature: 0.7}]"
    )
    spy = SpyProvider()
    await _pipeline(tmp_path, spy).run(_ctx(strategy="solo-cheap"), mode=Mode.BENCHMARK)
    assert [r.temperature for r in spy.requests] == [0.7]


async def test_benchmark_mode_never_runs_the_shadow_baseline(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path)
    real = await pipe.run(_ctx(shadow_baseline=True))
    bench = await pipe.run(_ctx(shadow_baseline=True), mode=Mode.BENCHMARK)
    assert real.shadow is not None and real.shadow.ran
    assert bench.shadow is None
    assert "shadow_baseline" not in _stages(bench)


def _display(pipe: Any, result: Any) -> str:
    presenter = pipe.deps.presenter
    usage = presenter.usage_for(result)
    return presenter.display_markdown(
        title="t",
        result=result,
        usage=usage,
        cost_comparison=presenter.comparison_for(result, usage),
        detail="full",
    )


async def test_lifetime_footer_is_real_mode_only(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path)
    await pipe.run(_ctx())  # the footer needs at least two stored runs
    real = await pipe.run(_ctx())
    bench = await pipe.run(_ctx(), mode=Mode.BENCHMARK)
    assert "Lifetime:" in _display(pipe, real)
    assert "Lifetime:" not in _display(pipe, bench)
    assert real.mode is Mode.REAL and bench.mode is Mode.BENCHMARK


async def test_benchmark_mode_does_not_trim_prompts_but_real_mode_does() -> None:
    entry = load_catalog().models["mock-fast"].model_copy(update={"context_window": 2_000})
    request = ModelRequest(model_id="mock-fast", user_prompt="x" * 50_000, max_tokens=100)
    seen: dict[str, int] = {}

    class Recorder(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            seen["chars"] = len(request.user_prompt)
            return await super().complete(request)

    for truncate in (True, False):
        gateway = CallGateway(
            ledger=RunLedger(),
            models={"mock-fast": entry},
            providers={"mock": Recorder(latency_ms=0.0)},
            pricing=PricingRegistry(),
            truncate_prompts=truncate,
        )
        await gateway.call(stage="solo", alias="mock-fast", request=request)
        assert (seen["chars"] < 50_000) is truncate
        assert bool(gateway.warnings) is truncate


async def test_the_full_ledger_is_stored_in_both_modes(tmp_path: Path) -> None:
    pipe = _pipeline(tmp_path)
    for mode in Mode:
        result = await pipe.run(_ctx(), mode=mode)
        stored = pipe.deps.run_store.get_run(result.run_id)
        assert stored is not None
        assert len(stored.output_data["ledger"]["calls"]) == len(result.ledger.records)
        assert stored.output_data["mode"] == mode.value
        assert stored.input_data["strategy"] is None and stored.input_data["mode"] == mode.value


# --------------------------------------------------------------------------- tools, CLI, config


async def test_fusion_ask_accepts_a_strategy_and_reports_it(tmp_path: Path) -> None:
    server = create_mcp_server(db_path=str(tmp_path / "runs.db"))
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        schema = tools["fusion_ask"].input_schema
        assert "strategy" in str(schema)
        result = await client.call_tool(
            "fusion_ask", {"input": {"prompt": PROMPT, "strategy": "solo-cheap"}}
        )
    assert result.data["routing"]["strategy"] == "solo-cheap"
    assert result.data["usage"]["successful_model_calls"] == 1


async def test_fusion_ask_unknown_strategy_is_an_error_the_caller_sees(tmp_path: Path) -> None:
    server = create_mcp_server(db_path=str(tmp_path / "runs.db"))
    async with Client(server) as client:
        with pytest.raises(Exception, match="Unknown strategy 'nope'"):
            await client.call_tool("fusion_ask", {"input": {"prompt": PROMPT, "strategy": "nope"}})


def test_pipeline_input_models_carry_the_strategy() -> None:
    assert FusionAskInput(prompt="x", strategy="solo-cheap").strategy == "solo-cheap"
    assert FusionAskInput(prompt="x").strategy is None


def test_cli_strategies_list_shows_every_strategy_and_budget_alias() -> None:
    result = CliRunner().invoke(app, ["strategies", "list"])
    assert result.exit_code == 0
    for name in ("solo-frontier", "panel-cheap", "panel-digest", "panel-refine"):
        assert name in result.output
    as_json = CliRunner().invoke(app, ["strategies", "list", "--json"])
    rows = yaml.safe_load(as_json.output)
    by_name = {row["name"]: row for row in rows}
    assert by_name["panel-duo"]["budgets"] == ["medium"]
    assert by_name["solo-cheap"]["members"][0]["model"] == "claude-haiku"


def test_cli_run_mock_takes_a_strategy(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["run-mock", "--strategy", "solo-cheap", "--db-path", str(tmp_path / "c.db")]
    )
    assert result.exit_code == 0, result.output
    assert "Run ID" in result.output


def test_cli_unknown_strategy_is_a_message_not_a_traceback(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["run-mock", "--strategy", "panel-cheep", "--db-path", str(tmp_path / "c.db")]
    )
    assert result.exit_code == 1
    assert "did you mean 'panel-cheap'" in result.output


def test_config_validate_flags_a_strategy_with_an_unknown_model(
    fusion_home: Path,
) -> None:
    (fusion_home / "config" / "config.yaml").write_text(
        yaml.safe_dump({"strategies": {"bad": {"kind": "solo", "members": [{"model": "ghost"}]}}})
    )
    result = CliRunner().invoke(app, ["config", "validate"])
    assert result.exit_code == 1
    assert "unknown model ghost" in result.output
