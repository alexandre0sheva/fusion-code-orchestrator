"""Stage isolation, ledger-based accounting in real runs, and the C2/C3/C9/C11 fixes."""

from __future__ import annotations

import ast
import asyncio
import inspect
import warnings
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from fusion.config.catalog import PriceSchedule, load_catalog
from fusion.config.loader import FanoutConfig, ModelEntry
from fusion.orchestration import factory
from fusion.orchestration.context import PipelineContext, RunState
from fusion.orchestration.factory import Settings, build_deps, build_pipelines
from fusion.orchestration.fanout import fanout_to_panel
from fusion.orchestration.pipelines import create_pipelines
from fusion.orchestration.prompts import (
    build_refinement_prompt,
    build_synthesis_prompt,
    build_user_prompt,
)
from fusion.orchestration.stages import (
    ContextEvalStage,
    JudgeStage,
    PanelStage,
    RedactStage,
    RouteStage,
    default_stages,
)
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.providers.mock import MockProvider
from fusion.routing.budget import BudgetLevel
from fusion.routing.classifier import TaskType
from fusion.routing.policy import Router
from fusion.telemetry.cost import PricingRegistry

REVIEW = (
    "diff --git a/app/db.py b/app/db.py\n--- a/app/db.py\n+++ b/app/db.py\n"
    '@@ -1 +1,2 @@\n+cursor.execute(f"SELECT * FROM t WHERE id={uid}")\n'
)


def _deps(
    tmp_path: Path,
    *,
    use_mock: bool = True,
    providers: dict[str, ModelProvider] | None = None,
    **kw: Any,
) -> Any:
    settings = Settings(use_mock=use_mock, db_path=str(tmp_path / "s.db"), **kw)
    return build_deps(settings, providers or {"mock": MockProvider(latency_ms=0.0)})


def _state(deps: Any, text: str = REVIEW, task: TaskType = TaskType.CODE_REVIEW, **kw: Any) -> Any:
    return RunState.start(PipelineContext(task_type=task, primary_content=text, **kw), deps)


# ------------------------------------------------------------------------------ stage isolation


async def test_redact_stage_sanitizes_input_and_opens_the_run(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    state = _state(deps, "review this " + "sk-ant-api03-" + "A" * 40 + "\n" + REVIEW)
    state = await RedactStage(deps).run(state)
    assert state.run_id
    assert "sk-ant-api03" not in state.sanitized_primary
    assert state.redaction_count >= 1
    assert deps.run_store.get_run(state.run_id) is not None
    deps.run_store.close()


async def test_route_stage_selects_models_and_falls_back_by_catalog_role(tmp_path: Path) -> None:
    class OpenAIOnly(MockProvider):
        name = "openai"

    deps = _deps(tmp_path, use_mock=False, providers={"openai": OpenAIOnly()})
    state = _state(deps)
    state = await RouteStage(deps).run(state)
    assert state.panel_models == ["gpt-luna"]  # the only panel member with a provider
    assert (
        state.judge_model == "gpt-luna"
    )  # gemini-flash is unavailable; next model with role judge
    assert any("falling back to gpt-luna" in w for w in state.warnings)
    deps.run_store.close()


async def test_context_eval_stage_halts_when_context_is_insufficient(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    state = _state(deps, "x")
    for stage in (RedactStage, RouteStage, ContextEvalStage):
        state = await stage(deps).run(state)
    assert state.halt is not None and state.halt.reason == "insufficient_context"
    assert state.final_answer == "Insufficient context for analysis."
    assert state.ledger.records == []
    deps.run_store.close()


async def test_panel_stage_records_every_call_in_the_ledger(tmp_path: Path) -> None:
    deps = _deps(tmp_path)
    state = _state(deps)
    for stage in (RedactStage, RouteStage, ContextEvalStage, PanelStage):
        state = await stage(deps).run(state)
    assert state.halt is None
    panel_calls = [r for r in state.ledger.records if r.stage == "panel"]
    assert sorted(r.model_alias for r in panel_calls) == sorted(state.panel_models)
    assert len(state.successful) == len(panel_calls)
    deps.run_store.close()


async def test_panel_stage_halts_without_quorum_and_keeps_the_diagnostics(tmp_path: Path) -> None:
    class Failing(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(
                provider="mock", model=request.model_id, error="down", error_type="Server"
            )

    deps = _deps(tmp_path, providers={"mock": Failing()})
    state = _state(deps)
    for stage in (RedactStage, RouteStage, ContextEvalStage, PanelStage):
        state = await stage(deps).run(state)
    assert state.halt is not None and state.halt.reason == "quorum"
    assert "quorum was not met" in state.final_answer
    assert state.structured["quorum_met"] is False
    assert all(not r.ok for r in state.ledger.records)
    deps.run_store.close()


async def test_judge_stage_runs_alone_on_a_prepared_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CHEAP__JUDGE", "light")
    deps = _deps(tmp_path)
    state = _state(deps)
    for stage in (RedactStage, RouteStage, ContextEvalStage, PanelStage):
        state = await stage(deps).run(state)
    state = await JudgeStage(deps).run(state)
    judge_calls = [r for r in state.ledger.records if r.stage == "judge"]
    assert len(judge_calls) == len(state.successful) == len(state.evaluations)
    assert {r.model_alias for r in judge_calls} == {state.judge_model}
    deps.run_store.close()


def test_default_stage_order() -> None:
    stages = default_stages(object())  # type: ignore[arg-type]
    names = [type(s).__name__ for s in stages]
    assert names == [
        "RedactStage",
        "RouteStage",
        "ContextEvalStage",
        "ShadowStartStage",
        "PanelStage",
        "RefineStage",
        "ClaimsStage",
        "ConcurrentStages",
        "FinalEvalStage",
        "ShadowStage",
        "PersistStage",
    ]
    concurrent = stages[names.index("ConcurrentStages")]
    assert [type(s).__name__ for s in concurrent.stages] == ["JudgeStage", "AggregateStage"]


# -------------------------------------------------------------------------- C2: judge cost counts


class TokenProvider(MockProvider):
    """Mock answers plus fixed token usage so every call has a price."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await super().complete(request)
        return response.model_copy(
            update={"input_tokens": 1000, "output_tokens": 500, "cost_estimate_usd": None}
        )


def _priced_mock_catalog() -> Any:
    catalog = load_catalog()
    price = PriceSchedule(
        input_per_1m=1.0,
        output_per_1m=2.0,
        verified_on=date(2026, 10, 5),
        source_url="https://example.test/p",
    )
    models = {
        alias: entry.model_copy(update={"prices": [price]}) if entry.provider == "mock" else entry
        for alias, entry in catalog.models.items()
    }
    return catalog.model_copy(update={"models": models})


async def test_judge_and_synthesis_cost_is_part_of_the_reported_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CHEAP__JUDGE", "light")
    pricing = PricingRegistry(_priced_mock_catalog())
    pipes = build_pipelines(
        Settings(use_mock=True, db_path=str(tmp_path / "c.db"), pricing=pricing),
        {"mock": TokenProvider(latency_ms=0.0)},
    )
    from fusion.orchestration.schemas import FusionAskInput

    out = await pipes["ask"].ask(FusionAskInput(prompt="How do I retry an HTTP call in python?"))
    per_call = (1000 * 1.0 + 500 * 2.0) / 1_000_000
    steps = out.cost_latency.steps
    judge_steps = [s for s in steps if s.step_name.startswith("judge:")]
    panel_steps = [s for s in steps if s.step_name.startswith("panel:")]
    assert judge_steps and len(judge_steps) == len(panel_steps)
    expected_calls = len(panel_steps) + len(judge_steps) + 1  # + synthesis
    assert out.cost_latency.total_cost_usd == pytest.approx(per_call * expected_calls)
    assert out.usage.successful_model_calls == expected_calls
    assert not any("not itemized" in w for w in out.cost_latency.warnings)
    pipes["ask"]._run_store.close()


# ------------------------------------------------------------------- C9: pending call attribution


class SlowFastProvider(ModelProvider):
    name = "mock"

    def is_available(self) -> bool:
        return True

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.model_id in {"mock-fast", "mock-weak"}:
            await asyncio.sleep(0.5)
        return ModelResponse(
            provider="mock", model=request.model_id, text="ok", input_tokens=1, output_tokens=1
        )


async def test_pending_calls_are_attributed_to_their_own_model() -> None:
    models = {n: load_catalog().models[n] for n in ("mock-fast", "mock-security", "mock-weak")}
    result = await fanout_to_panel(
        panel_models=["mock-fast", "mock-security", "mock-weak"],
        registry_models=models,
        providers={"mock": SlowFastProvider()},
        task_type=TaskType.CODE_REVIEW,
        primary_content=REVIEW,
        config=FanoutConfig(
            max_concurrency=3,
            per_model_timeout_seconds=5,
            global_timeout_seconds=0.05,
            min_successful_responses=1,
            cancel_on_global_timeout=False,
        ),
    )
    by_model = {c.model_name: c.status for c in result.calls}
    assert by_model == {"mock-fast": "timeout", "mock-security": "success", "mock-weak": "timeout"}
    assert len(result.calls) == 3


# ----------------------------------------------------------------- C11: no alias-substring magic


class PromptSpy(MockProvider):
    def __init__(self) -> None:
        super().__init__(latency_ms=0.0)
        self.system_prompts: dict[str, str] = {}

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.system_prompts[request.model_id] = request.system_prompt
        return await super().complete(request)


async def test_panel_persona_comes_from_the_catalog_not_the_alias() -> None:
    base = load_catalog().models["mock-fast"]
    models: dict[str, ModelEntry] = {
        "my-security-looking-model": base.model_copy(
            update={"alias": "my-security-looking-model", "model_id": "plain-1"}
        ),
        "explicit": base.model_copy(
            update={"alias": "explicit", "model_id": "explicit-1", "persona": "security_reviewer"}
        ),
    }
    spy = PromptSpy()
    await fanout_to_panel(
        panel_models=list(models),
        registry_models=models,
        providers={"mock": spy},
        task_type=TaskType.DEBUGGING,
        primary_content="boom",
    )
    assert "security-focused code reviewer" in spy.system_prompts["explicit-1"]
    plain = spy.system_prompts["plain-1"]
    assert "security-focused" not in plain
    assert "expert debugger" in plain  # falls back to the task's own system prompt


# ---------------------------------------------------------------------- C3: no silent truncation


def test_prompts_keep_long_inputs_intact() -> None:
    long_task, long_answer = "T" * 12_000, "A" * 9_000
    refine = build_refinement_prompt(
        task_type=TaskType.DEBUGGING,
        original_task=long_task,
        own_answer=long_answer,
        peer_answers=[("A", "P" * 9_000)],
    )
    assert long_task in refine and long_answer in refine and "P" * 9_000 in refine
    synth = build_synthesis_prompt(
        task_type=TaskType.DEBUGGING,
        panel_responses=[("m", long_answer)],
        disagreement_analysis={"notes": "n" * 5_000},
        original_task=long_task,
    )
    assert long_task in synth and long_answer in synth and "n" * 5_000 in synth
    assert long_task in build_user_prompt(task_type=TaskType.DEBUGGING, primary_content=long_task)


def test_no_fixed_size_prompt_slices_remain() -> None:
    import re

    root = Path(__file__).resolve().parents[1] / "src" / "fusion"
    offenders = []
    for name in ("orchestration/prompts.py", "evals/llm_judge.py", "benchmark/shadow.py"):
        text = (root / name).read_text()
        offenders += [f"{name}: {m}" for m in re.findall(r"\[:\d{4,}\]", text)]
    assert offenders == []


# -------------------------------------------------------------- test-mode branching is gone


def test_router_has_no_test_mode_branches() -> None:
    assert "test_mode" not in inspect.signature(Router.route).parameters
    source = (Path(__file__).resolve().parents[1] / "src/fusion/routing/policy.py").read_text()
    assert "is_test_mode" not in source


def test_mock_mode_is_selected_by_the_factory(tmp_path: Path) -> None:
    mock_deps = _deps(tmp_path)
    decision = mock_deps.routing.route(TaskType.CODE_REVIEW, budget=BudgetLevel.HIGH)
    assert decision.selected_panel and all(
        mock_deps.registry.get(m).provider == "mock" for m in decision.selected_panel
    )
    assert decision.judge_model == "mock-judge"
    live = build_deps(
        Settings(use_mock=False, db_path=str(tmp_path / "l.db")), {"openai": MockProvider()}
    )
    live_decision = live.routing.route(TaskType.CODE_REVIEW)
    assert all(live.registry.get(m).provider != "mock" for m in live_decision.selected_panel)
    mock_deps.run_store.close()
    live.run_store.close()


# ------------------------------------------------------------------------------- code structure


def test_no_function_in_orchestration_exceeds_120_lines() -> None:
    root = Path(__file__).resolve().parents[1] / "src/fusion/orchestration"
    long_ones = []
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                if length > 120:
                    long_ones.append(f"{path.name}:{node.name} ({length} lines)")
    assert long_ones == []


def test_pipelines_module_is_a_thin_reexport() -> None:
    source = Path(__file__).resolve().parents[1] / "src/fusion/orchestration/pipelines.py"
    assert len(source.read_text().splitlines()) < 120


def test_old_factory_names_still_work_but_warn(tmp_path: Path) -> None:
    with pytest.warns(DeprecationWarning, match="create_pipelines"):
        pipes = create_pipelines(
            providers={"mock": MockProvider(latency_ms=0.0)}, db_path=str(tmp_path / "d.db")
        )
    assert set(pipes) == {"ask", "code_review", "debug", "architecture", "plan", "answer_eval"}
    pipes["ask"]._run_store.close()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        built = build_pipelines(
            Settings(use_mock=True, db_path=str(tmp_path / "e.db")),
            {"mock": MockProvider(latency_ms=0.0)},
        )
    assert set(built) == set(pipes)
    built["ask"]._run_store.close()
    assert factory.build_provider_registry is not None
