"""Routing policy selection for orchestration tasks."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, Field

from fusion.config.env import is_test_mode
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

_CLOUD_FALLBACK_PANEL = ["gemini-flash", "claude-sonnet", "gpt-5.4-mini"]
_MOCK_PANEL = ["mock-fast", "mock-security", "mock-weak"]


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
        test_mode: bool | None = None,
        complexity: Complexity | None = None,
        risk: Risk | None = None,
    ) -> RoutingDecision:
        """Classify a task and select models respecting budget and constraints."""
        resolved_test_mode = is_test_mode(test_mode)

        task_type = self._classifier.classify(
            explicit_type=explicit_type,
            tool_name=tool_name,
            content=content,
        )
        resolved_complexity = complexity or self._classifier.estimate_complexity(content)
        resolved_risk = risk or self._classifier.estimate_risk(task_type=task_type, content=content)
        policy = self.get_policy(task_type)
        reasons: list[str] = []
        warnings: list[str] = []

        local_only = budget == BudgetLevel.LOCAL_ONLY
        if resolved_test_mode:
            reasons.append("Test mode active — preferring mock provider models")
            budget_panel = _MOCK_PANEL
            max_panel = policy.max_panel_size
            if resolved_risk == "high" and task_type == TaskType.CODE_REVIEW:
                budget_panel = list(_MOCK_PANEL)
                max_panel = policy.high_risk_max_panel_size
                reasons.append("High-risk code review — expanded mock panel")
        elif policy.budgets and budget.value in policy.budgets:
            budget_entry = policy.budgets[budget.value]
            budget_panel = budget_entry.panel_models or policy.panel_models
            max_panel = budget_entry.max_panel_size
            reasons.append(f"Applied {budget.value} budget policy")
        else:
            budget_panel = policy.panel_models
            max_panel = policy.max_panel_size
            reasons.append(f"Using default panel for {canonical_task_key(task_type)}")

        if resolved_risk == "high" and task_type == TaskType.CODE_REVIEW and not resolved_test_mode:
            budget_panel = policy.high_risk_panel_models or budget_panel
            max_panel = max(max_panel, policy.high_risk_max_panel_size)
            reasons.append("High-risk code review — expanded panel")

        if budget == BudgetLevel.LOW:
            max_panel = min(max_panel, 1)
            reasons.append("Low budget — limiting panel size")

        panel = self._registry.filter_candidates(
            budget_panel,
            budget=budget if not resolved_test_mode else BudgetLevel.HIGH,
            local_only=local_only and not resolved_test_mode,
        )
        if resolved_test_mode:
            panel = [m for m in panel if self._registry.get(m).provider == "mock"] or [
                "mock-fast"
            ]

        if not panel and local_only and not resolved_test_mode:
            warnings.append("No local models configured; falling back to cloud panel")
            panel = self._registry.filter_candidates(
                policy.panel_models or _CLOUD_FALLBACK_PANEL,
                budget=BudgetLevel.MEDIUM,
                local_only=False,
            )

        selected_panel = panel[:max_panel]
        if not selected_panel:
            fallback = _MOCK_PANEL if resolved_test_mode else _CLOUD_FALLBACK_PANEL
            selected_panel = self._registry.filter_candidates(
                fallback,
                budget=BudgetLevel.MEDIUM if not resolved_test_mode else BudgetLevel.HIGH,
                local_only=False,
            )[: max(1, max_panel)]
            warnings.append(
                "No eligible panel models; using "
                + ("mock fallback" if resolved_test_mode else "cloud fallback")
            )

        judge_candidates = [policy.judge_model]
        if policy.budgets and budget.value in policy.budgets:
            override = policy.budgets[budget.value].judge_model
            if override:
                judge_candidates.insert(0, override)

        judge_filtered = self._registry.filter_candidates(
            judge_candidates,
            budget=budget if not resolved_test_mode else BudgetLevel.HIGH,
            require_json=True,
            local_only=local_only and not resolved_test_mode,
        )
        if not judge_filtered:
            judge_filtered = self._registry.filter_candidates(
                ["gemini-flash", "claude-sonnet", "mock-judge"],
                budget=BudgetLevel.MEDIUM,
                require_json=True,
                local_only=False,
            )

        synth_candidates = [policy.synthesizer_model]
        if policy.budgets and budget.value in policy.budgets:
            override = policy.budgets[budget.value].synthesizer_model
            if override:
                synth_candidates.insert(0, override)

        if resolved_test_mode:
            judge_model = "mock-judge"
            synthesizer_model = "mock-judge"
        else:
            judge_model = judge_filtered[0] if judge_filtered else "gemini-flash"
            if judge_model != policy.judge_model:
                reasons.append(f"Selected JSON-capable judge: {judge_model}")

            synth_filtered = self._registry.filter_candidates(
                synth_candidates,
                budget=budget,
                local_only=local_only,
            )
            if not synth_filtered:
                synth_filtered = self._registry.filter_candidates(
                    ["claude-sonnet", "gpt-5.4-mini", "mock-judge"],
                    budget=BudgetLevel.MEDIUM,
                    local_only=False,
                )
            synthesizer_model = synth_filtered[0] if synth_filtered else judge_model

        cost_tiers = [
            self._registry.get(name).cost_tier
            for name in [*selected_panel, judge_model, synthesizer_model]
            if self._registry.is_enabled(name)
        ]
        estimated_cost_tier = max(cost_tiers, key=lambda t: {"low": 0, "medium": 1, "high": 2}[t])

        return RoutingDecision(
            task_type=task_type,
            complexity=cast(Complexity, resolved_complexity),
            risk=cast(Risk, resolved_risk),
            selected_panel=selected_panel,
            judge_model=judge_model,
            synthesizer_model=synthesizer_model,
            reasons=reasons,
            estimated_cost_tier=estimated_cost_tier,
            warnings=warnings,
        )


class RoutingPolicy:
    """Backward-compatible facade over Router."""

    def __init__(self, config: RoutingPoliciesConfig | None = None) -> None:
        self._router = Router(policies=config)

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
        test_mode: bool | None = None,
    ) -> list[str]:
        decision = self._router.route(
            explicit_type=task_type.value,
            content=content,
            budget=budget,
            test_mode=test_mode,
        )
        return decision.selected_panel

    def select_judge(
        self,
        task_type: TaskType,
        *,
        budget: BudgetLevel = BudgetLevel.MEDIUM,
        content: str = "",
        test_mode: bool | None = None,
    ) -> str:
        decision = self._router.route(
            explicit_type=task_type.value,
            content=content,
            budget=budget,
            test_mode=test_mode,
        )
        return decision.judge_model

    def select_synthesizer(
        self,
        task_type: TaskType,
        *,
        budget: BudgetLevel = BudgetLevel.MEDIUM,
        content: str = "",
        test_mode: bool | None = None,
    ) -> str:
        decision = self._router.route(
            explicit_type=task_type.value,
            content=content,
            budget=budget,
            test_mode=test_mode,
        )
        return decision.synthesizer_model

    def min_context_score(self, task_type: TaskType) -> float:
        return self.get_policy(task_type).min_context_score
