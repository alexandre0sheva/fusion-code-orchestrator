"""What a tool call does while it runs: stop on cancel, return a digest at the soft limit, obey a
cost cap, and serve over HTTP. Providers are scripted mocks; no clock is trusted beyond wide
margins."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from _scripted import FAST, PROMPT, Scripted, context, pipeline
from fusion.mcp_server.server import create_mcp_server, run_server
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.orchestration.progress import progress_sink, report
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.telemetry.cost import PricingRegistry


class Tracked(Scripted):
    """Counts calls that started, finished and were cancelled mid-flight."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.started = 0
        self.finished = 0
        self.cancelled = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.started += 1
        try:
            response = await super().complete(request)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        self.finished += 1
        return response


# ------------------------------------------------------------------------------ cancellation


async def test_cancelling_a_run_cancels_every_provider_call_in_flight(tmp_path: Path) -> None:
    provider = Tracked(delay={"panel": 30.0})
    task = asyncio.create_task(pipeline(tmp_path, provider).run(context(strategy="panel-cheap")))
    async with asyncio.timeout(10):
        while provider.started < 3:
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled == 3 and provider.finished == 0


async def test_cancelling_during_synthesis_cancels_the_synthesis_call(tmp_path: Path) -> None:
    provider = Tracked(delay={"synthesizer": 30.0})
    task = asyncio.create_task(pipeline(tmp_path, provider).run(context(strategy="panel-duo")))
    async with asyncio.timeout(10):
        while provider.models_called("synthesizer") == []:
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled == 1


async def test_a_cancelled_call_is_on_the_ledger_and_the_cancellation_goes_on() -> None:
    provider = Tracked(delay={"panel": 30.0})
    from fusion.config.catalog import load_catalog

    models = load_catalog().models
    gateway = CallGateway(
        ledger=RunLedger(), models=models, providers={"mock": provider}, pricing=PricingRegistry()
    )
    request = ModelRequest(
        model_id=models[FAST].model_id,
        system_prompt="s",
        user_prompt="u",
        metadata={"role": "panel"},
    )
    task = asyncio.create_task(gateway.call(stage="panel", alias=FAST, request=request))
    async with asyncio.timeout(5):
        while provider.started == 0:
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    records = gateway.ledger.records
    assert [(r.stage, r.status) for r in records] == [("panel", "cancelled")]
    assert not records[0].cost_known  # it may have been billed


# ------------------------------------------------------------------------------ the soft limit


async def test_a_slow_synthesis_returns_the_panel_digest_with_a_warning(tmp_path: Path) -> None:
    provider = Tracked(delay={"synthesizer": 30.0})
    pipe = pipeline(tmp_path, provider)
    pipe.soft_timeout_s = 0.5
    async with asyncio.timeout(20):
        result = await pipe.run(context(strategy="panel-cheap"))
    assert result.partial and result.halt_reason is None
    assert result.final_answer.startswith("## Panel digest")
    assert any(w.startswith("Soft time limit of 0s reached") for w in result.warnings)
    assert provider.cancelled == 1  # the synthesis call was cancelled, not left running
    stored = pipe.deps.run_store.get_run(result.run_id)
    assert stored is not None and stored.status == "completed"
    assert result.panel_results and result.claims  # scored from the panel's own answers


async def test_a_run_inside_the_limit_is_not_partial(tmp_path: Path) -> None:
    pipe = pipeline(tmp_path, Scripted())
    pipe.soft_timeout_s = 30.0
    result = await pipe.run(context(strategy="panel-duo"))
    assert not result.partial and not result.final_answer.startswith("## Panel digest")


async def test_a_partial_result_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__CACHE__ENABLED", "true")
    provider = Tracked(delay={"synthesizer": 30.0})
    pipe = pipeline(tmp_path, provider)
    pipe.soft_timeout_s = 0.5
    first = await pipe.run(context(strategy="panel-cheap"))
    second = await pipe.run(context(strategy="panel-cheap"))
    assert first.partial and second.partial and not second.cache_hit


async def test_with_no_answers_yet_the_run_stops_and_says_why(tmp_path: Path) -> None:
    provider = Tracked(delay={"panel": 30.0})
    pipe = pipeline(tmp_path, provider)
    pipe.soft_timeout_s = 0.3
    async with asyncio.timeout(20):
        result = await pipe.run(context(strategy="panel-cheap"))
    assert result.partial and result.halt_reason == "timeout"
    assert "No answer was ready" in result.final_answer
    assert provider.cancelled == 3 and provider.finished == 0


async def test_no_limit_means_the_run_waits(tmp_path: Path) -> None:
    pipe = pipeline(tmp_path, Scripted(delay={"synthesizer": 0.6}))
    assert pipe.soft_timeout_s is None
    result = await pipe.run(context(strategy="panel-cheap"))
    assert not result.partial


async def test_the_limit_never_applies_to_a_benchmark_run(tmp_path: Path) -> None:
    from fusion.orchestration.strategy import Mode

    pipe = pipeline(tmp_path, Scripted(delay={"synthesizer": 0.6}))
    pipe.soft_timeout_s = 0.2
    result = await pipe.run(context(strategy="panel-cheap"), mode=Mode.BENCHMARK)
    assert not result.partial


# ------------------------------------------------------------------------------ the cost cap


async def test_a_per_call_cap_that_cannot_be_met_stops_before_any_model_is_called(
    tmp_path: Path,
) -> None:
    provider = Scripted()
    pipe = pipeline(tmp_path, provider, priced=True)
    result = await pipe.run(context(strategy="panel-cheap", max_cost_usd=1e-9))
    assert result.halt_reason == "budget" and not provider.requests
    assert "Cost cap not met" in result.final_answer


async def test_a_roomy_cap_is_recorded_and_changes_nothing_else(tmp_path: Path) -> None:
    pipe = pipeline(tmp_path, Scripted(), priced=True)
    result = await pipe.run(context(strategy="panel-duo", max_cost_usd=5.0))
    assert result.halt_reason is None
    assert result.budget is not None and result.budget.max_cost_usd == 5.0


async def test_a_tight_cap_shifts_the_strategy_down_with_a_warning(tmp_path: Path) -> None:
    """The priced mock panel is forecast at about $0.06; one model alone at about $0.004."""
    provider = Scripted()
    pipe = pipeline(tmp_path, provider, priced=True)
    result = await pipe.run(context(strategy="panel-cheap", max_cost_usd=0.01))
    assert result.halt_reason is None and len(result.routing.selected_panel) == 1
    assert any("over its $0.0100 cap" in w for w in result.warnings)
    assert len(provider.models_called("panel")) == 1


async def test_the_lower_of_the_call_cap_and_the_strategy_cap_applies(tmp_path: Path) -> None:
    from fusion.orchestration.context import RunState

    pipe = pipeline(tmp_path, Scripted(), priced=True)
    strategy = pipe.deps.routing.strategies.get("panel-cheap")
    capped = strategy.model_copy(update={"max_cost_usd": 0.05})
    pipe.deps.routing.strategies.strategies["panel-cheap"] = capped
    for call_cap, expected in ((0.2, 0.05), (0.01, 0.01)):
        state = RunState.start(context(strategy="panel-cheap", max_cost_usd=call_cap), pipe.deps)
        assert state.strategy is not None and state.strategy.max_cost_usd == expected


# ------------------------------------------------------------------------------ progress sink


async def test_progress_without_a_listener_is_silent_and_a_broken_listener_is_harmless(
    capsys: pytest.CaptureFixture[str],
) -> None:
    await report("nobody is listening")

    async def broken(_message: str) -> None:
        raise RuntimeError("client went away")

    with progress_sink(broken):
        await report("this must not raise")
    captured = capsys.readouterr()
    assert captured.out == "" and "progress report failed" in captured.err


# ------------------------------------------------------------------------------ HTTP transport


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_the_http_transport_serves_the_same_tools_on_localhost(tmp_path: Path) -> None:
    server = create_mcp_server(db_path=str(tmp_path / "http.db"))
    port = _free_port()
    serving = asyncio.create_task(
        server.run_http_async(host="127.0.0.1", port=port, show_banner=False)
    )
    try:
        async with asyncio.timeout(30):
            while True:
                try:
                    async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                        tools = {t.name for t in await client.list_tools()}
                        result = await client.call_tool(
                            "fusion_ask", {"input": {"prompt": PROMPT, "strategy": "solo-cheap"}}
                        )
                    break
                except (OSError, RuntimeError):
                    await asyncio.sleep(0.1)
        assert "fusion_review_diff" in tools
        assert result.structured_content is not None and result.structured_content["run_id"]
    finally:
        serving.cancel()
        await asyncio.gather(serving, return_exceptions=True)


def test_http_refuses_a_public_address_unless_told_otherwise() -> None:
    for host in ("0.0.0.0", "192.168.1.20", "example.com"):  # noqa: S104
        with pytest.raises(ValueError, match="no authentication"):
            run_server(transport="http", host=host)


def test_the_command_line_reports_a_refused_address_and_a_bad_transport() -> None:
    from typer.testing import CliRunner

    from fusion.cli.app import app

    refused = CliRunner().invoke(app, ["mcp", "--transport", "http", "--host", "0.0.0.0"])  # noqa: S104
    assert refused.exit_code == 2 and "no authentication" in refused.output
    bad = CliRunner().invoke(app, ["mcp", "--transport", "carrier-pigeon"])
    assert bad.exit_code != 0
