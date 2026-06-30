"""Budget levels and tracking for cost and latency."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from fusion.config.loader import BudgetConfig, CostTier

LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio", "mock"})


class BudgetLevel(StrEnum):
    """Routing budget presets."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    LOCAL_ONLY = "local_only"


_COST_TIER_RANK: dict[CostTier, int] = {"low": 0, "medium": 1, "high": 2}


def cost_tier_within_budget(model_tier: CostTier, budget: BudgetLevel) -> bool:
    """Return True if a model's cost tier fits the selected budget."""
    if budget == BudgetLevel.LOCAL_ONLY:
        return True
    rank = _COST_TIER_RANK[model_tier]
    limits: dict[BudgetLevel, int] = {
        BudgetLevel.LOW: 0,
        BudgetLevel.MEDIUM: 1,
        BudgetLevel.HIGH: 2,
    }
    return rank <= limits[budget]


@dataclass
class BudgetTracker:
    """Tracks accumulated cost and latency against budgets."""

    config: BudgetConfig
    total_cost_usd: float = 0.0
    total_latency_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def record(self, *, cost_usd: float, latency_ms: float) -> None:
        self.total_cost_usd += cost_usd
        self.total_latency_ms += latency_ms
        if self.total_cost_usd >= self.config.warn_cost_usd:
            self.warnings.append(
                f"Cost warning: ${self.total_cost_usd:.4f} exceeds warn threshold"
            )

    def is_over_budget(self) -> bool:
        return (
            self.total_cost_usd > self.config.default_max_cost_usd
            or self.total_latency_ms > self.config.default_max_latency_ms
        )
