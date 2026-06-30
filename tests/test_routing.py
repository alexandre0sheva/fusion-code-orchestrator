"""Tests for routing and classification."""


from fusion.config.loader import load_model_registry
from fusion.routing.budget import BudgetLevel
from fusion.routing.classifier import TaskClassifier, TaskType, normalize_task_type
from fusion.routing.model_registry import ModelRegistry
from fusion.routing.policy import Router, RoutingPolicy


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


def test_disabled_models_not_selected() -> None:
    registry = ModelRegistry()
    router = Router(registry=registry)
    decision = router.route(
        explicit_type="architecture_decision",
        budget=BudgetLevel.HIGH,
        test_mode=False,
    )
    assert "claude-opus-disabled" not in decision.selected_panel
    assert "claude-opus-disabled" not in decision.judge_model


def test_local_only_falls_back_without_local_models() -> None:
    registry = ModelRegistry()
    router = Router(registry=registry)
    decision = router.route(
        explicit_type="code_review",
        budget=BudgetLevel.LOCAL_ONLY,
        test_mode=False,
    )
    assert "No local models configured" in " ".join(decision.warnings)
    for alias in decision.selected_panel:
        provider = registry.get(alias).provider
        assert provider in {"anthropic", "openai", "google"}


def test_high_risk_code_review_expands_panel() -> None:
    registry = ModelRegistry()
    router = Router(registry=registry)
    content = "security auth password production SQL injection vulnerability"
    decision = router.route(
        explicit_type="code_review",
        content=content,
        budget=BudgetLevel.HIGH,
        test_mode=True,
    )
    assert decision.risk == "high"
    assert len(decision.selected_panel) >= 2


def test_low_budget_selects_fewer_models() -> None:
    registry = ModelRegistry()
    router = Router(registry=registry)
    low = router.route(
        explicit_type="code_review",
        budget=BudgetLevel.LOW,
        test_mode=True,
    )
    high = router.route(
        explicit_type="code_review",
        budget=BudgetLevel.HIGH,
        test_mode=True,
    )
    assert len(low.selected_panel) <= len(high.selected_panel)


def test_routing_policy_selects_panel() -> None:
    routing = RoutingPolicy()
    panel = routing.select_panel(TaskType.CODE_REVIEW, test_mode=True)
    assert len(panel) >= 1
    assert "mock-fast" in panel


def test_routing_judge_and_synthesizer() -> None:
    routing = RoutingPolicy()
    assert routing.select_judge(TaskType.DEBUGGING, test_mode=True) == "mock-judge"
    assert routing.select_synthesizer(TaskType.IMPLEMENTATION_PLAN, test_mode=True) == "mock-judge"


def test_production_routing_uses_cloud_models() -> None:
    routing = RoutingPolicy()
    panel = routing.select_panel(TaskType.CODE_REVIEW, test_mode=False)
    assert "mock-fast" not in panel
    assert any(model in panel for model in ("claude-sonnet", "gpt-5.4-mini", "gemini-flash"))


def test_judge_prefers_json_capable_model() -> None:
    registry = ModelRegistry()
    router = Router(registry=registry)
    decision = router.route(
        explicit_type="answer_eval",
        budget=BudgetLevel.MEDIUM,
        test_mode=False,
    )
    judge = registry.get(decision.judge_model)
    assert judge.supports_json is True


def test_routing_decision_structure() -> None:
    router = Router()
    decision = router.route(explicit_type="debugging", content="stack trace error")
    assert decision.task_type == TaskType.DEBUGGING
    assert decision.complexity in {"low", "medium", "high"}
    assert decision.estimated_cost_tier in {"low", "medium", "high"}
    assert isinstance(decision.reasons, list)
