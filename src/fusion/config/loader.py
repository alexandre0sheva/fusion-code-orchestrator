"""Load YAML configuration files for routing, baselines and the model registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from fusion.config.catalog import Catalog, CostTier, ModelEntry, load_catalog
from fusion.config.layers import (
    ConfigError,
    ResolvedConfig,
    format_validation_error,
    resolve_config,
)

__all__ = [
    "BaselineConfig",
    "BaselineEntry",
    "BudgetConfig",
    "CacheConfig",
    "CostTier",
    "EarlyReturn",
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
_ROUTING_KEYS = frozenset({"policies", "budgets", "fanout", "refinement", "cache"})

class ModelRegistryConfig(BaseModel):
    """Full model registry loaded from YAML."""

    models: dict[str, ModelEntry]


_STRATEGY_HINT = "the panel and aggregator are set by strategies; see docs/CONFIGURATION.md"


def _reject_moved_keys(data: Any, moved: dict[str, str]) -> Any:
    """Point users of removed v0.1.0 settings at where they live now."""
    if isinstance(data, dict):
        found = [f"{key} ({hint})" for key, hint in moved.items() if key in data]
        if found:
            msg = f"no longer supported: {', '.join(found)}"
            raise ValueError(msg)
    return data


class RoutingPolicyEntry(BaseModel):
    """Per-task settings: which model judges answers and how much context is enough.

    Which models answer and aggregate is chosen by the run's strategy (``strategies`` section).
    """

    task_type: str
    judge_model: str = "gemini-flash"
    min_context_score: float = 0.3

    @model_validator(mode="before")
    @classmethod
    def _moved(cls, data: Any) -> Any:
        keys = (
            "panel_models",
            "max_panel_size",
            "high_risk_panel_models",
            "high_risk_max_panel_size",
            "budgets",
            "synthesizer_model",
        )
        return _reject_moved_keys(data, dict.fromkeys(keys, _STRATEGY_HINT))


class BudgetConfig(BaseModel):
    """Cost and latency budget defaults."""

    default_max_cost_usd: float = 1.0
    default_max_latency_ms: int = 120_000
    warn_cost_usd: float = 0.5


class EarlyReturn(BaseModel):
    """Stop waiting for stragglers once enough panelists have answered."""

    quorum: int = Field(default=2, ge=1)  # answers needed (never below min_successful_responses)
    grace_ms: float = Field(default=1500.0, ge=0)  # how long stragglers get after the quorum


class FanoutConfig(BaseModel):
    """Async panel fan-out controls."""

    max_concurrency: int = Field(default=6, ge=1)  # in-flight calls per provider
    per_model_timeout_seconds: float = Field(default=45.0, gt=0)
    global_timeout_seconds: float = Field(default=60.0, gt=0)
    min_successful_responses: int = Field(default=2, ge=1)
    cancel_on_global_timeout: bool = True
    allow_partial_results: bool = True
    early_return: EarlyReturn | None = None  # off unless set
    hedge_after_ms: float | None = Field(default=None, gt=0)  # off unless set


class RefinementConfig(BaseModel):
    """Mixture-of-agents refinement round controls."""

    per_model_timeout_seconds: float = Field(default=45.0, gt=0)
    global_timeout_seconds: float = Field(default=60.0, gt=0)
    min_panel_size: int = Field(default=2, ge=1)
    # Skip a round when the panel already agrees at least this much (None: always refine).
    skip_above_agreement: float | None = Field(default=0.8, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def _moved(cls, data: Any) -> Any:
        hint = "refinement rounds are set by a strategy's rounds; see docs/CONFIGURATION.md"
        return _reject_moved_keys(data, dict.fromkeys(("enabled_budgets", "max_rounds"), hint))


class CacheConfig(BaseModel):
    """Cache of whole answers for identical requests (real mode; off unless enabled)."""

    enabled: bool = False
    ttl_seconds: float = Field(default=900.0, gt=0)
    max_entries: int = Field(default=128, ge=1)


class RoutingPoliciesConfig(BaseModel):
    """Full routing policies loaded from YAML."""

    policies: dict[str, RoutingPolicyEntry]
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    fanout: FanoutConfig = Field(default_factory=FanoutConfig)
    refinement: RefinementConfig = Field(default_factory=RefinementConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)


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
    """Load the model registry (the catalog's models)."""
    return ModelRegistryConfig(models=load_catalog(path).models)


def load_routing_policies(path: Path | None = None) -> RoutingPoliciesConfig:
    """Load routing policies: layered configuration, or one explicit file."""
    if path is not None:
        return RoutingPoliciesConfig.model_validate(_load_yaml(path))
    resolved = resolve_config()
    section = {k: v for k, v in resolved.data.items() if k in _ROUTING_KEYS}
    try:
        return RoutingPoliciesConfig.model_validate(section)
    except ValidationError as exc:
        raise format_validation_error(exc, section_prefix="", resolved=resolved) from exc


def load_baseline(path: Path | None = None, catalog: Catalog | None = None) -> BaselineConfig:
    """Load baseline configs, resolving provider and model ID from the catalog."""
    resolved: ResolvedConfig | None = None
    if path is not None:
        raw = _load_yaml(path)
    else:
        resolved = resolve_config()
        raw = {"baselines": resolved.data.get("baselines", [])}
    resolved_catalog = catalog or load_catalog()
    entries: list[BaselineEntry] = []
    for index, item in enumerate(raw.get("baselines", [])):
        try:
            entry = BaselineEntry.model_validate(item)
        except ValidationError as exc:
            if resolved is None:
                raise
            raise format_validation_error(
                exc, section_prefix=f"baselines.{index}", resolved=resolved
            ) from exc
        if entry.model:
            if entry.model not in resolved_catalog.models:
                msg = f"baselines.{index} ({entry.name}) references unknown model '{entry.model}'"
                raise ConfigError(msg)
            model = resolved_catalog.get(entry.model)
            entry = entry.model_copy(
                update={"provider": model.provider, "model_id": model.model_id}
            )
        entries.append(entry)
    return BaselineConfig(baselines=entries) if entries else BaselineConfig()
