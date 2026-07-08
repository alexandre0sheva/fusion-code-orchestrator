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


class PricingEntry(BaseModel):
    """Published or estimated model pricing in USD per one million tokens."""

    provider: str
    model_id: str
    alias: str
    input_price_per_1m_tokens: float | None = Field(default=None, ge=0)
    output_price_per_1m_tokens: float | None = Field(default=None, ge=0)
    cached_input_price_per_1m_tokens: float | None = Field(default=None, ge=0)
    reasoning_price_per_1m_tokens: float | None = Field(default=None, ge=0)
    currency: str = "USD"
    source_notes: str = ""
    updated_at: str = ""
    is_estimate: bool = True


class PricingConfig(BaseModel):
    """Pricing registry keyed by provider/model alias."""

    pricing: dict[str, PricingEntry] = Field(default_factory=dict)


class BaselineEntry(BaseModel):
    """Single frontier model baseline used for cost comparison."""

    name: str = "Opus 4.8"
    provider: str = "anthropic"
    model_id: str | None = "claude-opus-4-8"
    pricing_alias: str = "anthropic.claude-opus-4-8"
    description: str = "Single frontier model baseline for comparison"
    enabled: bool = True
    estimate_strategy: str = "same_input_and_output_tokens"


class BaselineConfig(BaseModel):
    """Baseline config wrapper."""

    baseline: BaselineEntry = Field(default_factory=BaselineEntry)


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


def load_pricing(path: Path | None = None) -> PricingConfig:
    """Load model pricing registry from YAML."""
    config_path = path or (_CONFIG_DIR / "pricing.yaml")
    return PricingConfig.model_validate(_load_yaml(config_path))


def load_baseline(path: Path | None = None) -> BaselineConfig:
    """Load baseline model comparison config from YAML."""
    config_path = path or (_CONFIG_DIR / "baseline.yaml")
    return BaselineConfig.model_validate(_load_yaml(config_path))
