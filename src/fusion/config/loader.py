"""Load YAML configuration files for models and routing."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

_CONFIG_DIR = Path(__file__).parent

CostTier = Literal["low", "medium", "high"]
LatencyTier = Literal["low", "medium", "high"]
ContextTier = Literal["small", "medium", "long"]
QualityTier = Literal["weak", "medium", "strong", "frontier"]
BudgetLevelName = Literal["low", "medium", "high", "local_only"]


class ModelEntry(BaseModel):
    """A single model definition in the registry."""

    alias: str = ""
    provider: str
    model_id: str
    enabled: bool = True
    strengths: list[str] = Field(default_factory=list)
    cost_tier: CostTier = "medium"
    latency_tier: LatencyTier = "medium"
    context_tier: ContextTier = "medium"
    quality_tier: QualityTier = "medium"
    supports_json: bool = False
    supports_tools: bool | None = None
    notes: str = ""
    max_tokens: int = 4096
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    display_name: str = ""
    capabilities: list[str] = Field(default_factory=list)


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


class RoutingPoliciesConfig(BaseModel):
    """Full routing policies loaded from YAML."""

    policies: dict[str, RoutingPolicyEntry]
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        msg = f"Expected dict in {path}"
        raise ValueError(msg)
    return data


def load_model_registry(path: Path | None = None) -> ModelRegistryConfig:
    """Load the model registry from YAML."""
    config_path = path or (_CONFIG_DIR / "default_models.yaml")
    raw = _load_yaml(config_path)
    models_raw = raw.get("models", {})
    models: dict[str, ModelEntry] = {}
    for alias, entry_data in models_raw.items():
        if not isinstance(entry_data, dict):
            continue
        entry = ModelEntry.model_validate({**entry_data, "alias": alias})
        models[alias] = entry
    return ModelRegistryConfig(models=models)


def load_routing_policies(path: Path | None = None) -> RoutingPoliciesConfig:
    """Load routing policies from YAML."""
    config_path = path or (_CONFIG_DIR / "routing_policies.yaml")
    return RoutingPoliciesConfig.model_validate(_load_yaml(config_path))
