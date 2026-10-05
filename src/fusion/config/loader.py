"""Load YAML configuration files for routing, baselines and the model registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from fusion.config.catalog import Catalog, CostTier, ModelEntry, load_catalog

__all__ = [
    "BaselineConfig",
    "BaselineEntry",
    "BudgetConfig",
    "CostTier",
    "FanoutConfig",
    "ModelEntry",
    "ModelRegistryConfig",
    "RefinementConfig",
    "RoutingPoliciesConfig",
    "load_baseline",
    "load_model_registry",
    "load_routing_policies",
]

_CONFIG_DIR = Path(__file__).parent

BudgetLevelName = Literal["low", "medium", "high", "local_only"]


class ModelRegistryConfig(BaseModel):
    """Full model registry loaded from YAML."""

    models: dict[str, ModelEntry]


class BudgetPolicyEntry(BaseModel):
    """Budget-specific model selection overrides."""

    panel_models: list[str] = Field(default_factory=list)
    max_panel_size: int = 1
    judge_model: str | None = None
    synthesizer_model: str | None = None


class RoutingPolicyEntry(BaseModel):
    """Routing policy for a task type."""

    task_type: str
    panel_models: list[str] = Field(default_factory=list)
    judge_model: str = "gemini-flash"
    synthesizer_model: str = "claude-sonnet"
    max_panel_size: int = 3
    min_context_score: float = 0.3
    high_risk_panel_models: list[str] = Field(default_factory=list)
    high_risk_max_panel_size: int = 4
    budgets: dict[str, BudgetPolicyEntry] = Field(default_factory=dict)


class BudgetConfig(BaseModel):
    """Cost and latency budget defaults."""

    default_max_cost_usd: float = 1.0
    default_max_latency_ms: int = 120_000
    warn_cost_usd: float = 0.5


class FanoutConfig(BaseModel):
    """Async panel fan-out controls."""

    max_concurrency: int = Field(default=6, ge=1)
    per_model_timeout_seconds: float = Field(default=45.0, gt=0)
    global_timeout_seconds: float = Field(default=60.0, gt=0)
    min_successful_responses: int = Field(default=2, ge=1)
    cancel_on_global_timeout: bool = True
    allow_partial_results: bool = True


class RefinementConfig(BaseModel):
    """Mixture-of-agents refinement round controls."""

    enabled_budgets: list[BudgetLevelName] = Field(default_factory=list)
    per_model_timeout_seconds: float = Field(default=45.0, gt=0)
    global_timeout_seconds: float = Field(default=60.0, gt=0)
    min_panel_size: int = Field(default=2, ge=1)
    max_rounds: int = Field(default=1, ge=0)

    def enabled_for(self, budget: str) -> bool:
        return self.max_rounds > 0 and budget in self.enabled_budgets


class RoutingPoliciesConfig(BaseModel):
    """Full routing policies loaded from YAML."""

    policies: dict[str, RoutingPolicyEntry]
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    fanout: FanoutConfig = Field(default_factory=FanoutConfig)
    refinement: RefinementConfig = Field(default_factory=RefinementConfig)


class BaselineEntry(BaseModel):
    """A frontier model that Fusion's cost and quality are compared against."""

    name: str = "Opus 5.5"
    model: str | None = None  # catalog alias; provider/model_id are resolved from it
    provider: str = "anthropic"
    model_id: str | None = "claude-opus-5-5"
    description: str = "Single frontier model baseline for comparison"
    enabled: bool = True
    estimate_strategy: str = "same_input_and_output_tokens"


class BaselineConfig(BaseModel):
    """Configured baselines; the first enabled one is the default."""

    baselines: list[BaselineEntry] = Field(default_factory=lambda: [BaselineEntry()])

    @property
    def baseline(self) -> BaselineEntry:
        for entry in self.baselines:
            if entry.enabled:
                return entry
        return self.baselines[0]


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        msg = f"Expected dict in {path}"
        raise ValueError(msg)
    return data


def load_model_registry(path: Path | None = None) -> ModelRegistryConfig:
    """Load the model registry (the catalog's models) from YAML."""
    return ModelRegistryConfig(models=load_catalog(path).models)


def load_routing_policies(path: Path | None = None) -> RoutingPoliciesConfig:
    """Load routing policies from YAML."""
    config_path = path or (_CONFIG_DIR / "routing_policies.yaml")
    return RoutingPoliciesConfig.model_validate(_load_yaml(config_path))


def load_baseline(path: Path | None = None, catalog: Catalog | None = None) -> BaselineConfig:
    """Load baseline configs, resolving provider and model ID from the catalog."""
    config_path = path or (_CONFIG_DIR / "baseline.yaml")
    raw = _load_yaml(config_path)
    resolved_catalog = catalog or load_catalog()
    entries: list[BaselineEntry] = []
    for item in raw.get("baselines", []):
        entry = BaselineEntry.model_validate(item)
        if entry.model:
            model = resolved_catalog.get(entry.model)
            entry = entry.model_copy(
                update={"provider": model.provider, "model_id": model.model_id}
            )
        entries.append(entry)
    return BaselineConfig(baselines=entries) if entries else BaselineConfig()
