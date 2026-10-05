"""Routing policy selection for orchestration tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, Field

from fusion.config.layers import ConfigError
from fusion.config.loader import RoutingPoliciesConfig, RoutingPolicyEntry, load_routing_policies
from fusion.routing.budget import BudgetLevel
from fusion.routing.classifier import (
    TaskClassifier,
    TaskType,
    canonical_task_key,
)
from fusion.routing.model_registry import ModelRegistry

if TYPE_CHECKING:  # orchestration imports routing, so these are imported lazily at run time
    from fusion.orchestration.strategy import Strategy, StrategyBook

Complexity = Literal["low", "medium", "high"]
Risk = Literal["low", "medium", "high"]

_COST_RANK = {"low": 0, "medium": 1, "high": 2}


class RoutingDecision(BaseModel):
    """Structured output from the router."""

    task_type: TaskType
    complexity: Complexity
    risk: Risk
    strategy: str = ""
    selected_panel: list[str] = Field(default_factory=list)
    judge_model: str
    synthesizer_model: str  # empty when the strategy calls no aggregator model
    reasons: list[str] = Field(default_factory=list)
    estimated_cost_tier: str
    warnings: list[str] = Field(default_factory=list)


def mock_routing_policies(
    config: RoutingPoliciesConfig, registry: ModelRegistry
) -> RoutingPoliciesConfig:
    """Point every policy's judge at the registry's mock judge, keeping the rest.

    Offline mode is a different *configuration*, not a different code path: the Router never asks
    whether it is running under test.
    """
    judge = (registry.by_role("judge") or registry.by_role("panel"))[0]
    policies = {
        key: policy.model_copy(update={"judge_model": judge})
        for key, policy in config.policies.items()
    }
    return config.model_copy(update={"policies": policies})


class Router:
    """Classifies a task and selects the judge and models for the run's strategy."""

    def __init__(
        self,
        *,
        registry: ModelRegistry | None = None,
        policies: RoutingPoliciesConfig | None = None,
        classifier: TaskClassifier | None = None,
    ) -> None:
        self._registry = registry or ModelRegistry()
        self._policies = policies or load_routing_policies()
        self._classifier = classifier or TaskClassifier()

    @property
    def budgets(self) -> RoutingPoliciesConfig:
        return self._policies

    def get_policy(self, task_type: TaskType) -> RoutingPolicyEntry:
        key = canonical_task_key(task_type)
        if key in self._policies.policies:
            return self._policies.policies[key]
        return self._policies.policies["default"]

    def route(
        self,
        *,
        strategy: Strategy,
        explicit_type: str | None = None,
        tool_name: str | None = None,
        content: str = "",
        complexity: Complexity | None = None,
        risk: Risk | None = None,
    ) -> RoutingDecision:
        """Classify a task and pick the panel, aggregator and judge models for ``strategy``."""
        task_type = self._classifier.classify(
            explicit_type=explicit_type, tool_name=tool_name, content=content
        )
        resolved_complexity = complexity or self._classifier.estimate_complexity(content)
        resolved_risk = risk or self._classifier.estimate_risk(task_type=task_type, content=content)
        policy = self.get_policy(task_type)
        reasons = [f"Applied strategy {strategy.name}"]
        warnings: list[str] = []

        panel = self._select_panel(strategy, warnings)
        judge_model = self._select_judge(policy)
        synthesizer_model = self._select_synthesizer(strategy, judge_model)

        used = [*panel, synthesizer_model] if synthesizer_model else list(panel)
        if strategy.judge != "off":
            used.append(judge_model)
        cost_tiers = [
            self._registry.get(name).cost_tier for name in used if name in self._registry.models
        ]
        return RoutingDecision(
            task_type=task_type,
            complexity=cast(Complexity, resolved_complexity),
            risk=cast(Risk, resolved_risk),
            strategy=strategy.name,
            selected_panel=panel,
            judge_model=judge_model,
            synthesizer_model=synthesizer_model,
            reasons=reasons,
            estimated_cost_tier=max(cost_tiers, key=lambda t: _COST_RANK[t], default="low"),
            warnings=warnings,
        )

    def _require_known(self, strategy: Strategy) -> None:
        for alias in strategy.models:
            if alias not in self._registry.models:
                msg = f"Strategy '{strategy.name}' references unknown model '{alias}'"
                raise ConfigError(msg)

    def _select_panel(self, strategy: Strategy, warnings: list[str]) -> list[str]:
        self._require_known(strategy)
        panel = [m.model for m in strategy.members if self._registry.is_enabled(m.model)]
        if panel:
            return panel
        fallback = self._registry.by_role("panel")[: len(strategy.members)]
        warnings.append(
            f"Strategy '{strategy.name}' has no enabled models "
            f"({', '.join(m.model for m in strategy.members)}); "
            "using the catalog's panel-role models"
        )
        return fallback

    def _select_judge(self, policy: RoutingPolicyEntry) -> str:
        chosen = [
            alias
            for alias in (policy.judge_model, *self._registry.by_role("judge"))
            if alias in self._registry.models
            and self._registry.is_enabled(alias)
            and self._registry.get(alias).supports_json
        ]
        return chosen[0] if chosen else policy.judge_model

    def _select_synthesizer(self, strategy: Strategy, judge_model: str) -> str:
        """The llm aggregator's model; empty when the strategy makes no aggregator call."""
        if strategy.kind == "solo" or strategy.aggregator != "llm":
            return ""
        if strategy.aggregator_model and self._registry.is_enabled(strategy.aggregator_model):
            return strategy.aggregator_model
        by_role = self._registry.by_role("synthesizer")
        return by_role[0] if by_role else judge_model


def build_routing(*, use_mock: bool) -> RoutingPolicy:
    """Routing for live mode, or for offline mode (mock models only)."""
    from fusion.orchestration.strategy import load_strategy_book, mock_strategy_book

    registry = ModelRegistry.for_mode(use_mock=use_mock)
    config = load_routing_policies()
    strategies = load_strategy_book()
    if use_mock:
        config = mock_routing_policies(config, registry)
        strategies = mock_strategy_book(strategies, registry)
    return RoutingPolicy(config, registry=registry, strategies=strategies)


class RoutingPolicy:
    """Routing facade: the router, the per-task policies and the strategy book."""

    def __init__(
        self,
        config: RoutingPoliciesConfig | None = None,
        *,
        registry: ModelRegistry | None = None,
        strategies: StrategyBook | None = None,
    ) -> None:
        self._router = Router(policies=config, registry=registry)
        if strategies is None:
            from fusion.orchestration.strategy import load_strategy_book

            strategies = load_strategy_book()
        self._strategies = strategies

    def get_policy(self, task_type: TaskType) -> RoutingPolicyEntry:
        return self._router.get_policy(task_type)

    @property
    def budgets(self) -> RoutingPoliciesConfig:
        return self._router.budgets

    @property
    def router(self) -> Router:
        return self._router

    @property
    def strategies(self) -> StrategyBook:
        return self._strategies

    def resolve_strategy(self, name: str | None, budget: BudgetLevel) -> Strategy:
        """An explicit strategy name wins; otherwise the strategy mapped to the budget."""
        return self._strategies.resolve(name, budget)

    def route(
        self,
        task_type: TaskType,
        *,
        strategy: str | None = None,
        budget: BudgetLevel = BudgetLevel.MEDIUM,
        content: str = "",
    ) -> RoutingDecision:
        return self._router.route(
            strategy=self.resolve_strategy(strategy, budget),
            explicit_type=task_type.value,
            content=content,
        )

    def select_panel(
        self,
        task_type: TaskType,
        *,
        strategy: str | None = None,
        budget: BudgetLevel = BudgetLevel.MEDIUM,
        content: str = "",
    ) -> list[str]:
        return self.route(
            task_type, strategy=strategy, budget=budget, content=content
        ).selected_panel

    def select_judge(
        self,
        task_type: TaskType,
        *,
        strategy: str | None = None,
        budget: BudgetLevel = BudgetLevel.MEDIUM,
        content: str = "",
    ) -> str:
        return self.route(task_type, strategy=strategy, budget=budget, content=content).judge_model

    def select_synthesizer(
        self,
        task_type: TaskType,
        *,
        strategy: str | None = None,
        budget: BudgetLevel = BudgetLevel.MEDIUM,
        content: str = "",
    ) -> str:
        return self.route(
            task_type, strategy=strategy, budget=budget, content=content
        ).synthesizer_model

    def min_context_score(self, task_type: TaskType) -> float:
        return self.get_policy(task_type).min_context_score
