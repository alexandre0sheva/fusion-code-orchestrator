"""Tests for shadow baseline A/B comparison and stats aggregation."""

from __future__ import annotations

import json
import random

import pytest

from fusion.benchmark.shadow import (
    run_shadow_comparison,
    shadow_mode,
    should_run_shadow,
)
from fusion.config.loader import BaselineEntry
from fusion.orchestration.pipelines import PipelineContext, create_pipeline
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.providers.mock import MockProvider
from fusion.routing.budget import BudgetLevel
from fusion.routing.classifier import TaskType
from fusion.routing.model_registry import ModelRegistry
from fusion.storage.run_store import RunStore
from fusion.telemetry.stats_format import format_stats_markdown, stats_to_dict

_MOCK_BASELINE = BaselineEntry(
    name="Mock Baseline",
    provider="mock",
    model_id="mock-fast",
    pricing_alias="mock.mock-fast",
)


class ScriptedJudgeProvider(MockProvider):
    """Mock provider whose shadow-judge verdict is fixed."""

    def __init__(self, winner: str) -> None:
        super().__init__(latency_ms=1.0)
        self._winner = winner

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("role") == "shadow_judge":
            verdict = {
                "winner": self._winner,
                "answer_1_score": 0.9 if self._winner == "1" else 0.6,
                "answer_2_score": 0.9 if self._winner == "2" else 0.6,
                "reason": "scripted",
            }
            return ModelResponse(
                provider="mock",
                model=request.model_id,
                text=json.dumps(verdict),
                parsed_json=verdict,
                input_tokens=10,
                output_tokens=10,
                latency_ms=1.0,
            )
        return await super().complete(request)

    def is_available(self) -> bool:
        return True


class FailingBaselineProvider(MockProvider):
    """Fails only shadow-baseline calls."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("role") == "shadow_baseline":
            return ModelResponse(
                provider="mock", model=request.model_id, error="down", error_type="TestError"
            )
        return await super().complete(request)


def test_shadow_mode_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FUSION_SHADOW_MODE", raising=False)
    assert shadow_mode() == "off"
    monkeypatch.setenv("FUSION_SHADOW_MODE", "always")
    assert shadow_mode() == "always"
    monkeypatch.setenv("FUSION_SHADOW_MODE", "nonsense")
    assert shadow_mode() == "off"


def test_should_run_shadow_explicit_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_SHADOW_MODE", "always")
    assert should_run_shadow(None) is True
    assert should_run_shadow(False) is False
    monkeypatch.setenv("FUSION_SHADOW_MODE", "off")
    assert should_run_shadow(None) is False
    assert should_run_shadow(True) is True


def test_should_run_shadow_sampled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_SHADOW_MODE", "sampled")
    monkeypatch.setenv("FUSION_SHADOW_SAMPLE_RATE", "1.0")
    assert should_run_shadow(None, rng=random.Random(1)) is True
    monkeypatch.setenv("FUSION_SHADOW_SAMPLE_RATE", "0.0")
    assert should_run_shadow(None, rng=random.Random(1)) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("winner", "fusion_first", "expected"),
    [
        ("1", True, "fusion"),
        ("1", False, "baseline"),
        ("2", True, "baseline"),
        ("2", False, "fusion"),
        ("tie", True, "tie"),
    ],
)
async def test_blind_judge_winner_mapping(winner: str, fusion_first: bool, expected: str) -> None:
    registry = ModelRegistry()

    class FixedRandom(random.Random):
        def random(self) -> float:  # noqa: A003
            return 0.0 if fusion_first else 0.99

    result = await run_shadow_comparison(
        task_prompt="review this diff",
        system_prompt="you are a reviewer",
        fusion_answer="fusion answer",
        fusion_cost_usd=0.01,
        fusion_latency_ms=1000.0,
        registry_models=registry.models,
        providers={"mock": ScriptedJudgeProvider(winner)},
        judge_model_alias="mock-judge",
        baseline=_MOCK_BASELINE,
        rng=FixedRandom(),
    )
    assert result.ran
    assert result.winner == expected
    if expected == "fusion":
        assert result.fusion_score == 0.9
        assert result.baseline_score == 0.6
    elif expected == "baseline":
        assert result.fusion_score == 0.6
        assert result.baseline_score == 0.9


@pytest.mark.asyncio
async def test_shadow_baseline_failure_degrades_to_warning() -> None:
    registry = ModelRegistry()
    result = await run_shadow_comparison(
        task_prompt="review this diff",
        system_prompt="you are a reviewer",
        fusion_answer="fusion answer",
        fusion_cost_usd=0.01,
        fusion_latency_ms=1000.0,
        registry_models=registry.models,
        providers={"mock": FailingBaselineProvider()},
        judge_model_alias="mock-judge",
        baseline=_MOCK_BASELINE,
    )
    assert not result.ran
    assert result.winner == "error"
    assert any("baseline call failed" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_shadow_missing_provider_skips() -> None:
    registry = ModelRegistry()
    result = await run_shadow_comparison(
        task_prompt="task",
        system_prompt="sys",
        fusion_answer="answer",
        fusion_cost_usd=None,
        fusion_latency_ms=None,
        registry_models=registry.models,
        providers={},
        judge_model_alias="mock-judge",
        baseline=_MOCK_BASELINE,
    )
    assert not result.ran
    assert any("unavailable" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_pipeline_shadow_stores_comparison_and_stats(tmp_path) -> None:
    db_path = str(tmp_path / "shadow.db")
    pipeline = create_pipeline(db_path=db_path, use_mock=True)
    ctx = PipelineContext(
        task_type=TaskType.CODE_REVIEW,
        primary_content="diff: +def foo():\n+    return eval(user_input)",
        context="Security sensitive change with plenty of surrounding context.",
        budget=BudgetLevel.MEDIUM,
        shadow_baseline=True,
    )
    result = await pipeline.run(ctx)
    assert result.shadow is not None
    assert result.shadow.ran
    assert result.shadow.winner in {"fusion", "baseline", "tie"}

    store = RunStore(db_path=db_path)
    rows = store.list_shadow_comparisons()
    assert len(rows) == 1
    assert rows[0].run_id == result.run_id

    stats = store.get_stats()
    assert stats.total_runs == 1
    assert stats.shadow_total == 1
    assert stats.shadow_win_rate_percent is not None

    markdown = format_stats_markdown(stats, rows)
    assert "Shadow A/B" in markdown
    data = stats_to_dict(stats, rows)
    assert data["shadow_total"] == 1
    assert len(data["recent_shadow"]) == 1


@pytest.mark.asyncio
async def test_pipeline_no_shadow_by_default(tmp_path) -> None:
    db_path = str(tmp_path / "noshadow.db")
    pipeline = create_pipeline(db_path=db_path, use_mock=True)
    ctx = PipelineContext(
        task_type=TaskType.CODE_REVIEW,
        primary_content="diff: +def foo():\n+    return 1",
        context="Plenty of surrounding context for this small change.",
        budget=BudgetLevel.MEDIUM,
    )
    result = await pipeline.run(ctx)
    assert result.shadow is None
    assert RunStore(db_path=db_path).list_shadow_comparisons() == []


def test_stats_empty_db(tmp_path) -> None:
    store = RunStore(db_path=str(tmp_path / "empty.db"))
    stats = store.get_stats()
    assert stats.total_runs == 0
    assert stats.shadow_total == 0
    assert stats.shadow_win_rate_percent is None
    assert "No shadow comparisons yet" in format_stats_markdown(stats)
