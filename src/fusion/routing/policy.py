"""Routing policy selection for orchestration tasks."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, Field

from fusion.config.loader import RoutingPoliciesConfig, RoutingPolicyEntry, load_routing_policies
from fusion.routing.budget import BudgetLevel
from fusion.routing.classifier import (
    TaskClassifier,
    TaskType,
    canonical_task_key,
)
from fusion.routing.model_registry import ModelRegistry

Complexity = Literal["low", "medium", "high"]
Risk = Literal["low", "medium", "high"]

_COST_RANK = {"low": 0, "medium": 1, "high": 2}


class RoutingDecision(BaseModel):
    """Structured output from the router."""

    task_type: TaskType
    complexity: Complexity
    risk: Risk
    selected_panel: list[str] = Field(default_factory=list)
    judge_model: str
    synthesizer_model: str
    reasons: list[str] = Field(default_factory=list)
    estimated_cost_tier: str
    warnings: list[str] = Field(default_factory=list)


def mock_routing_policies(
    config: RoutingPoliciesConfig, registry: ModelRegistry
) -> RoutingPoliciesConfig:
    """Point every policy at the registry's mock models, keeping sizes, budgets and fan-out.

    Offline mode is a different *configuration*, not a different code path: the Router never asks
    whether it is running under test.
    """
    panel = registry.by_role("panel")
    judge = (registry.by_role("judge") or panel)[0]
    synthesizer = (registry.by_role("synthesizer") or [judge])[0]
    policies = {}
    for key, policy in config.policies.items():
        budgets = {
            name: entry.model_copy(
                update={
                    "panel_models": panel,
                    # Offline panels are sized by the policy, not per budget (as before).
                    "max_panel_size": policy.max_panel_size,
                    "judge_model": judge if entry.judge_model else None,
                    "synthesizer_model": synthesizer if entry.synthesizer_model else None,
                }
            )
            for name, entry in policy.budgets.items()
        }
        policies[key] = policy.model_copy(
            update={
                "panel_models": panel,
                "high_risk_panel_models": panel if policy.high_risk_panel_models else [],
                "judge_model": judge,
                "synthesizer_model": synthesizer,
                "budgets": budgets,
            }
        )
    return config.model_copy(update={"policies": policies})


class Router:
    """Selects panel, judge, and synthesizer models for a task."""

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
        explicit_type: str | None = None,
        tool_name: str | None = None,
        content: str = "",
        budget: BudgetLevel = BudgetLevel.MEDIUM,
        complexity: Complexity | None = None,
        risk: Risk | None = None,
    ) -> RoutingDecision:
        """Classify a task and select models respecting budget and constraints."""
        task_type = self._classifier.classify(
            explicit_type=explicit_type, tool_name=tool_name, content=content
        )
        resolved_complexity = complexity or self._classifier.estimate_complexity(content)
        resolved_risk = risk or self._classifier.estimate_risk(task_type=task_type, content=content)
        policy = self.get_policy(task_type)
        reasons: list[str] = []
        warnings: list[str] = []

        panel = self._select_panel(policy, task_type, resolved_risk, budget, reasons, warnings)
        judge_model = self._select_judge(policy, budget, reasons)
        synthesizer_model = self._select_synthesizer(policy, budget, judge_model)

        cost_tiers = [
            self._registry.get(name).cost_tier
            for name in [*panel, judge_model, synthesizer_model]
            if self._registry.is_enabled(name)
        ]
        return RoutingDecision(
            task_type=task_type,
            complexity=cast(Complexity, resolved_complexity),
            risk=cast(Risk, resolved_risk),
            selected_panel=panel,
            judge_model=judge_model,
            synthesizer_model=synthesizer_model,
            reasons=reasons,
            estimated_cost_tier=max(cost_tiers, key=lambda t: _COST_RANK[t]),
            warnings=warnings,
        )

    def _select_panel(
        self,
        policy: RoutingPolicyEntry,
        task_type: TaskType,
        risk: str,
        budget: BudgetLevel,
        reasons: list[str],
        warnings: list[str],
    ) -> list[str]:
        local_only = budget == BudgetLevel.LOCAL_ONLY
        if policy.budgets and budget.value in policy.budgets:
            budget_entry = policy.budgets[budget.value]
            candidates = budget_entry.panel_models or policy.panel_models
            max_panel = budget_entry.max_panel_size
            reasons.append(f"Applied {budget.value} budget policy")
        else:
            candidates = policy.panel_models
            max_panel = policy.max_panel_size
            reasons.append(f"Using default panel for {canonical_task_key(task_type)}")

        if risk == "high" and task_type == TaskType.CODE_REVIEW:
            candidates = policy.high_risk_panel_models or candidates
            max_panel = max(max_panel, policy.high_risk_max_panel_size)
            reasons.append("High-risk code review — expanded panel")
        if budget == BudgetLevel.LOW:
            max_panel = min(max_panel, 1)
            reasons.append("Low budget — limiting panel size")

        panel = self._registry.filter_candidates(candidates, budget=budget, local_only=local_only)
        if not panel and local_only:
            warnings.append("No local models configured; falling back to cloud panel")
            panel = self._registry.filter_candidates(
                policy.panel_models or self._registry.by_role("panel"),
                budget=BudgetLevel.MEDIUM,
                local_only=False,
            )
        selected = panel[:max_panel]
        if not selected:
            selected = self._registry.filter_candidates(
                self._registry.by_role("panel"), budget=BudgetLevel.MEDIUM, local_only=False
            )[: max(1, max_panel)]
            warnings.append("No eligible panel models; using the catalog's panel-role models")
        return selected

    def _select_judge(
        self, policy: RoutingPolicyEntry, budget: BudgetLevel, reasons: list[str]
    ) -> str:
        candidates = [policy.judge_model]
        if policy.budgets and budget.value in policy.budgets:
            override = policy.budgets[budget.value].judge_model
            if override:
                candidates.insert(0, override)
        local_only = budget == BudgetLevel.LOCAL_ONLY
        chosen = self._registry.filter_candidates(
            candidates, budget=budget, require_json=True, local_only=local_only
        )
        if not chosen:
            chosen = self._registry.filter_candidates(
                self._registry.by_role("judge"),
                budget=BudgetLevel.MEDIUM,
                require_json=True,
                local_only=False,
            )
        judge = chosen[0] if chosen else policy.judge_model
        if judge != policy.judge_model:
            reasons.append(f"Selected JSON-capable judge: {judge}")
        return judge

    def _select_synthesizer(
        self, policy: RoutingPolicyEntry, budget: BudgetLevel, judge_model: str
    ) -> str:
        candidates = [policy.synthesizer_model]
        if policy.budgets and budget.value in policy.budgets:
            override = policy.budgets[budget.value].synthesizer_model
            if override:
                candidates.insert(0, override)
        chosen = self._registry.filter_candidates(
            candidates, budget=budget, local_only=budget == BudgetLevel.LOCAL_ONLY
        )
        if not chosen:
            chosen = self._registry.filter_candidates(
                self._registry.by_role("synthesizer"), budget=BudgetLevel.MEDIUM, local_only=False
            )
        return chosen[0] if chosen else judge_model


def build_routing(*, use_mock: bool) -> RoutingPolicy:
    """Routing for live mode, or for offline mode (mock models only)."""
    registry = ModelRegistry.for_mode(use_mock=use_mock)
    config = load_routing_policies()
    if use_mock:
        config = mock_routing_policies(config, registry)
    return RoutingPolicy(config, registry=registry)


class RoutingPolicy:
    """Backward-compatible facade over Router."""

    def __init__(
        self, config: RoutingPoliciesConfig | None = None, *, registry: ModelRegistry | None = None
    ) -> None:
        self._router = Router(policies=config, registry=registry)

    def get_policy(self, task_type: TaskType) -> RoutingPolicyEntry:
        return self._router.get_policy(task_type)

    @property
    def budgets(self) -> RoutingPoliciesConfig:
        return self._router.budgets

    @property
    def router(self) -> Router:
        return self._router

    def route(self, **kwargs: object) -> RoutingDecision:
        return self._router.route(**kwargs)  # type: ignore[arg-type]

    def select_panel(
        self,
        task_type: TaskType,
        *,
        budget: BudgetLevel = BudgetLevel.MEDIUM,
        content: str = "",
    ) -> list[str]:
        decision = self._router.route(
            explicit_type=task_type.value,
            content=content,
            budget=budget,
        )
        return decision.selected_panel

    def select_judge(
        self,
        task_type: TaskType,
        *,
        budget: BudgetLevel = BudgetLevel.MEDIUM,
        content: str = "",
    ) -> str:
        decision = self._router.route(
            explicit_type=task_type.value,
            content=content,
            budget=budget,
        )
        return decision.judge_model

    def select_synthesizer(
        self,
        task_type: TaskType,
        *,
        budget: BudgetLevel = BudgetLevel.MEDIUM,
        content: str = "",
    ) -> str:
        decision = self._router.route(
            explicit_type=task_type.value,
            content=content,
            budget=budget,
        )
        return decision.synthesizer_model

    def min_context_score(self, task_type: TaskType) -> float:
        return self.get_policy(task_type).min_context_score
