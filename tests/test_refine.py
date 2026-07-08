"""Tests for the mixture-of-agents refinement round."""

from __future__ import annotations

import pytest

from fusion.config.loader import RefinementConfig, load_routing_policies
from fusion.orchestration.pipelines import PipelineContext, create_pipeline
from fusion.orchestration.refine import refine_panel_responses
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.providers.mock import MockProvider
from fusion.routing.budget import BudgetLevel
from fusion.routing.classifier import TaskType
from fusion.routing.model_registry import ModelRegistry


class FailingProvider(ModelProvider):
    """Provider that always errors, to exercise the fallback path."""

    name = "mock"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            provider="mock",
            model=request.model_id,
            error="boom",
            error_type="TestError",
        )

    def is_available(self) -> bool:
        return True


def _round1_responses() -> list[tuple[str, ModelResponse]]:
    return [
        ("mock-fast", ModelResponse(provider="mock", model="mock-fast", text="answer one")),
        ("mock-security", ModelResponse(provider="mock", model="mock-security", text="answer two")),
    ]


@pytest.mark.asyncio
async def test_refinement_replaces_answers_on_success() -> None:
    registry = ModelRegistry()
    responses = _round1_responses()
    refined, result = await refine_panel_responses(
        responses=responses,
        registry_models=registry.models,
        providers={"mock": MockProvider()},
        task_type=TaskType.CODE_REVIEW,
        original_task="review this diff",
        config=RefinementConfig(enabled_budgets=["high"]),
    )
    assert result.ran
    assert result.refined_count == 2
    assert [name for name, _ in refined] == ["mock-fast", "mock-security"]
    # Refined answers come from a second model call, not the originals.
    assert refined[0][1].text != "answer one"


@pytest.mark.asyncio
async def test_refinement_failure_keeps_round1_answer() -> None:
    registry = ModelRegistry()
    responses = _round1_responses()
    refined, result = await refine_panel_responses(
        responses=responses,
        registry_models=registry.models,
        providers={"mock": FailingProvider()},
        task_type=TaskType.CODE_REVIEW,
        original_task="review this diff",
        config=RefinementConfig(enabled_budgets=["high"]),
    )
    assert result.ran
    assert result.refined_count == 0
    assert refined[0][1].text == "answer one"
    assert refined[1][1].text == "answer two"
    assert any("kept round-1" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_refinement_skipped_below_min_panel_size() -> None:
    registry = ModelRegistry()
    responses = _round1_responses()[:1]
    refined, result = await refine_panel_responses(
        responses=responses,
        registry_models=registry.models,
        providers={"mock": MockProvider()},
        task_type=TaskType.CODE_REVIEW,
        original_task="review this diff",
        config=RefinementConfig(enabled_budgets=["high"], min_panel_size=2),
    )
    assert not result.ran
    assert refined == responses


def test_refinement_config_loaded_from_yaml() -> None:
    config = load_routing_policies()
    assert config.refinement.enabled_for("high")
    assert not config.refinement.enabled_for("medium")
    assert not config.refinement.enabled_for("low")


@pytest.mark.asyncio
async def test_pipeline_runs_refinement_at_high_budget(tmp_path) -> None:
    pipeline = create_pipeline(db_path=str(tmp_path / "t.db"), use_mock=True)
    ctx = PipelineContext(
        task_type=TaskType.CODE_REVIEW,
        primary_content="diff: +def foo():\n+    return eval(user_input)",
        context="Security sensitive change with plenty of surrounding context.",
        budget=BudgetLevel.HIGH,
    )
    result = await pipeline.run(ctx)
    assert result.refinement is not None
    assert result.refinement.ran
    step_names = [step.step_name for step in result.trace.steps]
    assert any(name.startswith("refine:") for name in step_names)
    # Refinement usage rows are aggregated into the run usage summary.
    aliases = [u.model_alias for u in result.usage.per_model]
    assert len(aliases) > len(result.panel_results) + 1  # panel + refine + synthesis


@pytest.mark.asyncio
async def test_pipeline_skips_refinement_at_medium_budget(tmp_path) -> None:
    pipeline = create_pipeline(db_path=str(tmp_path / "t.db"), use_mock=True)
    ctx = PipelineContext(
        task_type=TaskType.CODE_REVIEW,
        primary_content="diff: +def foo():\n+    return 1",
        context="Plenty of surrounding context for this small change.",
        budget=BudgetLevel.MEDIUM,
    )
    result = await pipeline.run(ctx)
    assert result.refinement is None
    step_names = [step.step_name for step in result.trace.steps]
    assert not any(name.startswith("refine:") for name in step_names)
