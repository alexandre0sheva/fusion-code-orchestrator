"""Tests for routing and classification."""

import pytest

from fusion.config.layers import ConfigError
from fusion.config.loader import load_model_registry
from fusion.orchestration.strategy import PanelMember, Strategy, load_strategy_book
from fusion.routing.budget import BudgetLevel
from fusion.routing.classifier import TaskClassifier, TaskType, normalize_task_type
from fusion.routing.model_registry import ModelRegistry
from fusion.routing.policy import Router, build_routing


def test_classifier_explicit_type() -> None:
    classifier = TaskClassifier()
    assert classifier.classify(explicit_type="code_review") == TaskType.CODE_REVIEW


def test_classifier_tool_name() -> None:
    classifier = TaskClassifier()
    assert classifier.classify(tool_name="fusion_review_diff") == TaskType.CODE_REVIEW
    assert classifier.classify(tool_name="fusion_plan_feature") == TaskType.IMPLEMENTATION_PLAN


def test_classifier_heuristic_debug() -> None:
    classifier = TaskClassifier()
    result = classifier.classify(content="Exception in thread stack trace error")
    assert result == TaskType.DEBUGGING


def test_classifier_heuristic_review() -> None:
    classifier = TaskClassifier()
    result = classifier.classify(content="Please review this diff in the pull request")
    assert result == TaskType.CODE_REVIEW


def test_normalize_legacy_aliases() -> None:
    assert normalize_task_type("architecture") == TaskType.ARCHITECTURE_DECISION
    assert normalize_task_type("planning") == TaskType.IMPLEMENTATION_PLAN
    assert normalize_task_type("evaluation") == TaskType.ANSWER_EVAL


def test_model_registry_loads() -> None:
    registry = ModelRegistry()
    assert "mock-fast" in registry.models
    entry = registry.get("mock-fast")
    assert entry.provider == "mock"
    assert entry.enabled is True
    assert entry.cost_tier == "low"


def test_registry_yaml_fields() -> None:
    config = load_model_registry()
    entry = config.models["claude-sonnet"]
    assert entry.quality_tier == "strong"
    assert entry.supports_json is True
    assert "code_review" in entry.strengths


def _strategy(*aliases: str, **kw: object) -> Strategy:
    kind = "solo" if len(aliases) == 1 else "panel"
    return Strategy(
        name="t",
        kind=kind,
        members=[PanelMember(model=a) for a in aliases],
        **kw,  # type: ignore[arg-type]
    )


def test_disabled_models_are_not_selected() -> None:
    router = Router(registry=ModelRegistry())
    decision = router.route(
        explicit_type="architecture_decision",
        strategy=_strategy("claude-opus-disabled", "claude-haiku"),
    )
    assert decision.selected_panel == ["claude-haiku"]


def test_unknown_strategy_model_is_a_config_error() -> None:
    router = Router(registry=ModelRegistry())
    with pytest.raises(ConfigError, match="unknown model 'nope'"):
        router.route(explicit_type="debugging", strategy=_strategy("claude-haiku", "nope"))


def test_strategy_with_no_enabled_models_falls_back_to_the_panel_role() -> None:
    registry = ModelRegistry()
    router = Router(registry=registry)
    decision = router.route(explicit_type="code_review", strategy=_strategy("claude-opus-disabled"))
    assert "has no enabled models" in " ".join(decision.warnings)
    assert decision.selected_panel
    assert all("panel" in registry.get(a).roles for a in decision.selected_panel)


def test_local_strategy_without_local_models_warns_and_uses_cloud_models() -> None:
    routing = build_routing(use_mock=False)
    decision = routing.route(TaskType.CODE_REVIEW, budget=BudgetLevel.LOCAL_ONLY)
    assert decision.strategy == "panel-local"
    assert "has no enabled models" in " ".join(decision.warnings)
    assert all(
        routing.router._registry.get(a).provider in {"anthropic", "openai", "google"}
        for a in decision.selected_panel
    )


def test_decision_reports_the_resolved_strategy_and_its_models() -> None:
    routing = build_routing(use_mock=False)
    decision = routing.route(TaskType.CODE_REVIEW, strategy="panel-cheap-strong-synth")
    assert decision.strategy == "panel-cheap-strong-synth"
    assert decision.selected_panel == ["claude-haiku", "gpt-luna", "gemini-flash"]
    assert decision.synthesizer_model == "claude-sonnet"
    solo = routing.route(TaskType.CODE_REVIEW, strategy="solo-frontier")
    assert solo.selected_panel == ["claude-opus"]
    assert solo.synthesizer_model == ""  # a solo run calls no aggregator


def test_budget_levels_map_to_strategies() -> None:
    routing = build_routing(use_mock=False)
    names = {
        budget: routing.route(TaskType.DEBUGGING, budget=budget).strategy for budget in BudgetLevel
    }
    assert names == {
        BudgetLevel.LOW: "solo-cheap",
        BudgetLevel.MEDIUM: "panel-duo",
        BudgetLevel.HIGH: "panel-refine",
        BudgetLevel.LOCAL_ONLY: "panel-local",
    }
    assert (
        routing.route(TaskType.DEBUGGING, strategy="solo-luna", budget=BudgetLevel.HIGH).strategy
        == "solo-luna"
    )


def test_low_budget_selects_fewer_models_than_high() -> None:
    routing = build_routing(use_mock=True)
    low = routing.route(TaskType.CODE_REVIEW, budget=BudgetLevel.LOW)
    high = routing.route(TaskType.CODE_REVIEW, budget=BudgetLevel.HIGH)
    assert len(low.selected_panel) == 1 < len(high.selected_panel)


def test_routing_policy_selects_panel() -> None:
    routing = build_routing(use_mock=True)
    panel = routing.select_panel(TaskType.CODE_REVIEW)
    assert len(panel) >= 1
    assert "mock-fast" in panel


def test_routing_judge_and_synthesizer() -> None:
    routing = build_routing(use_mock=True)
    assert routing.select_judge(TaskType.DEBUGGING) == "mock-judge"
    assert routing.select_synthesizer(TaskType.IMPLEMENTATION_PLAN) == "mock-judge"


def test_production_routing_uses_cloud_models() -> None:
    routing = build_routing(use_mock=False)
    panel = routing.select_panel(TaskType.CODE_REVIEW)
    assert "mock-fast" not in panel
    assert set(panel) == {"claude-haiku", "gpt-luna"}  # the default panel-duo


def test_judge_prefers_json_capable_model() -> None:
    registry = ModelRegistry()
    router = Router(registry=registry)
    strategy = load_strategy_book().get("panel-cheap")
    decision = router.route(explicit_type="answer_eval", strategy=strategy)
    assert registry.get(decision.judge_model).supports_json is True


def test_routing_decision_structure() -> None:
    router = Router()
    strategy = load_strategy_book().get("panel-cheap")
    decision = router.route(
        explicit_type="debugging", content="stack trace error", strategy=strategy
    )
    assert decision.task_type == TaskType.DEBUGGING
    assert decision.complexity in {"low", "medium", "high"}
    assert decision.estimated_cost_tier in {"low", "medium", "high"}
    assert isinstance(decision.reasons, list)


def test_frontier_strategy_reports_a_high_cost_tier() -> None:
    router = Router()
    strategy = load_strategy_book().get("solo-frontier")
    assert router.route(explicit_type="debugging", strategy=strategy).estimated_cost_tier == "high"
