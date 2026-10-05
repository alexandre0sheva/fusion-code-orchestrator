"""What a study needs of one pipeline run: its own seed, redaction, ledger and halt reason."""

from __future__ import annotations

from pathlib import Path

import pytest

from _scripted import PROMPT, context, pipeline
from fusion.orchestration.context import PipelineContext
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.orchestration.strategy import Mode
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.providers.mock import MockProvider
from fusion.routing.classifier import TaskType
from fusion.telemetry.cost import PricingRegistry

SECRET = "sk-ant-api03-" + "B" * 40


class Spy(MockProvider):
    def __init__(self) -> None:
        super().__init__(latency_ms=0.0)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


def _secret_ctx() -> PipelineContext:
    return context(f"{PROMPT} The service reads ANTHROPIC_API_KEY={SECRET} at startup.")


def _leaked(spy: Spy) -> bool:
    return any(SECRET in r.user_prompt for r in spy.requests)


async def test_real_mode_redacts_and_benchmark_mode_does_not(tmp_path: Path) -> None:
    for mode, leaks in ((Mode.REAL, False), (Mode.BENCHMARK, True)):
        spy = Spy()
        await pipeline(tmp_path, spy).run(_secret_ctx(), mode=mode)
        assert _leaked(spy) is leaks, mode


async def test_redaction_can_be_forced_either_way(tmp_path: Path) -> None:
    kept, scrubbed = Spy(), Spy()
    await pipeline(tmp_path / "a", kept).run(_secret_ctx(), mode=Mode.REAL, redact=False)
    await pipeline(tmp_path / "b", scrubbed).run(_secret_ctx(), mode=Mode.BENCHMARK, redact=True)
    assert _leaked(kept) and not _leaked(scrubbed)


async def test_a_run_can_choose_its_seed(tmp_path: Path) -> None:
    spy = Spy()
    await pipeline(tmp_path, spy).run(context(), mode=Mode.BENCHMARK, seed=41)
    assert spy.requests and {r.seed for r in spy.requests} == {41}
    spy.requests.clear()
    await pipeline(tmp_path, spy).run(context(), mode=Mode.BENCHMARK)
    assert {r.seed for r in spy.requests} == {0}  # the mode's own default


async def test_benchmark_calls_stream_and_real_calls_do_not(tmp_path: Path) -> None:
    spy = Spy()
    pipe = pipeline(tmp_path, spy)
    await pipe.run(context(), mode=Mode.BENCHMARK)
    assert spy.requests and all(r.stream for r in spy.requests)
    spy.requests.clear()
    await pipe.run(context())
    assert all(not r.stream for r in spy.requests)


async def test_models_that_cannot_stream_are_never_asked_to() -> None:
    from fusion.config.catalog import load_catalog

    entry = load_catalog().models["mock-fast"].model_copy(update={"supports_streaming": False})
    spy = Spy()
    gateway = CallGateway(
        ledger=RunLedger(),
        models={"mock-fast": entry},
        providers={"mock": spy},
        pricing=PricingRegistry(),
        stream=True,
    )
    await gateway.call(
        stage="solo", alias="mock-fast", request=ModelRequest(model_id="mock-fast", user_prompt="x")
    )
    assert spy.requests[0].stream is False


async def test_a_caller_keeps_the_ledger_when_a_stage_fails(tmp_path: Path) -> None:
    class SynthesizerDown(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("role") == "synthesizer":
                return ModelResponse(
                    provider="mock", model=request.model_id, error="down", error_type="Server"
                )
            return await super().complete(request)

    ledger = RunLedger()
    pipe = pipeline(tmp_path, SynthesizerDown(latency_ms=0.0))
    with pytest.raises(Exception, match="down"):
        await pipe.run(context(), mode=Mode.BENCHMARK, ledger=ledger)
    stages = [r.stage for r in ledger.records]
    assert stages.count("panel") == 3 and "synthesis" in stages  # the panel's calls are recorded


async def test_a_normal_run_has_no_halt_reason(tmp_path: Path) -> None:
    result = await pipeline(tmp_path).run(context())
    assert result.halt_reason is None


async def test_a_panel_without_quorum_halts_with_that_reason(tmp_path: Path) -> None:
    class Down(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(
                provider="mock", model=request.model_id, error="down", error_type="Server"
            )

    result = await pipeline(tmp_path, Down(latency_ms=0.0)).run(context())
    assert result.halt_reason == "quorum"


async def test_halted_runs_say_why(tmp_path: Path) -> None:
    short = PipelineContext(task_type=TaskType.CODE_REVIEW, primary_content="Short.")
    # A short task has no context at all: below the policy's minimum, so nothing is asked.
    result = await pipeline(tmp_path).run(short)
    assert result.halt_reason == "insufficient_context"
    assert result.ledger is not None and result.ledger.records == []
