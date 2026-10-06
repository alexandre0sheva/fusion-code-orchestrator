"""Model catalog: the single source of truth for model IDs, capabilities and prices.

Prices carry provenance (``verified_on`` + ``source_url``) and optional effective dates so a
scheduled provider price change (for example an introductory rate ending) is modeled
explicitly instead of silently going stale.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, TypeVar

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

if TYPE_CHECKING:
    from fusion.config.layers import ResolvedConfig

_CONFIG_DIR = Path(__file__).parent
_ModelT = TypeVar("_ModelT", bound=BaseModel)

CostTier = Literal["low", "medium", "high"]
LatencyTier = Literal["low", "medium", "high"]
ContextTier = Literal["small", "medium", "long"]
QualityTier = Literal["weak", "medium", "strong", "frontier"]

# Providers that have no per-token bill; a missing price block means "free".
FREE_PROVIDERS = frozenset({"mock", "ollama", "lmstudio"})


class PriceSchedule(BaseModel):
    """USD prices per one million tokens, valid for an optional date range."""

    input_per_1m: float = Field(ge=0)
    output_per_1m: float = Field(ge=0)
    cached_input_per_1m: float | None = Field(default=None, ge=0)
    cache_write_per_1m: float | None = Field(default=None, ge=0)
    # Only set when reasoning tokens are billed separately from output tokens.
    reasoning_per_1m: float | None = Field(default=None, ge=0)
    currency: str = "USD"
    effective_from: date | None = None
    effective_until: date | None = None
    verified_on: date | None = None
    source_url: str = ""
    is_estimate: bool = False

    def covers(self, on: date) -> bool:
        if self.effective_from is not None and on < self.effective_from:
            return False
        return self.effective_until is None or on <= self.effective_until


class ModelEntry(BaseModel):
    """A single model definition in the catalog."""

    alias: str = ""
    provider: str
    model_id: str
    enabled: bool = True
    display_name: str = ""
    notes: str = ""
    roles: list[str] = Field(default_factory=list)  # panel, judge, synthesizer, baseline
    # Optional panel persona (a key of the role prompts, e.g. security_reviewer). Models without
    # one answer with the task's own system prompt.
    persona: str | None = None
    strengths: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    cost_tier: CostTier = "medium"
    latency_tier: LatencyTier = "medium"
    context_tier: ContextTier = "medium"
    quality_tier: QualityTier = "medium"
    # Per-call output cap Fusion requests (not the model's hard limit).
    max_tokens: int = 4096
    context_window: int | None = None
    max_output: int | None = None
    supports_json: bool = False
    supports_json_schema: bool = False
    supports_tools: bool | None = None
    supports_vision: bool = False
    supports_streaming: bool = True
    supports_reasoning_effort: bool = False
    default_reasoning_effort: str | None = None
    # False means Fusion must not send temperature/top_p/top_k (some models answer 400).
    supports_sampling_params: bool = True
    retirement_not_before: date | None = None
    prices: list[PriceSchedule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_prices(self) -> Self:
        if self.provider in FREE_PROVIDERS:
            if not self.prices:
                self.prices = [PriceSchedule(input_per_1m=0.0, output_per_1m=0.0)]
            return self
        if not self.prices:
            msg = f"Model {self.alias or self.model_id} has no prices"
            raise ValueError(msg)
        for schedule in self.prices:
            if schedule.verified_on is None or not schedule.source_url:
                msg = (
                    f"Model {self.alias or self.model_id}: every price needs "
                    "verified_on and source_url"
                )
                raise ValueError(msg)
        return self

    def price_at(self, on: date | None = None) -> PriceSchedule | None:
        """Return the price schedule in effect on a date, or None if none applies."""
        day = on or date.today()
        covering = [p for p in self.prices if p.covers(day)]
        if not covering:
            return None
        return max(covering, key=lambda p: p.effective_from or date.min)


class ProviderLimits(BaseModel):
    """Client-side limits for one provider; ``None`` means unlimited."""

    max_concurrent: int | None = Field(default=None, ge=1)
    rpm: int | None = Field(default=None, ge=1)


class Catalog(BaseModel):
    """All configured models keyed by alias, plus per-provider rate limits."""

    models: dict[str, ModelEntry] = Field(default_factory=dict)
    provider_limits: dict[str, ProviderLimits] = Field(default_factory=dict)

    def get(self, alias: str) -> ModelEntry:
        if alias not in self.models:
            msg = f"Unknown model: {alias}"
            raise KeyError(msg)
        return self.models[alias]

    def find(self, provider: str, model_id: str) -> ModelEntry | None:
        """First catalog entry for a provider model ID (aliases share one price)."""
        for entry in self.models.values():
            if entry.provider == provider and entry.model_id == model_id:
                return entry
        return None


def catalog_from_raw(
    raw: dict[str, Any], source: str = "catalog", resolved: ResolvedConfig | None = None
) -> Catalog:
    """Build a catalog from a mapping with ``models`` and optional ``provider_limits``.

    When ``resolved`` is given, validation errors name the key and the layer that set it.
    """
    from fusion.config.layers import format_validation_error

    if not isinstance(raw.get("models"), dict):
        msg = f"Expected a 'models' mapping in {source}"
        raise ValueError(msg)

    def build(kind: type[_ModelT], section: str, data: dict[str, Any]) -> _ModelT:
        try:
            return kind.model_validate(data)
        except ValidationError as exc:
            if resolved is None:
                raise
            raise format_validation_error(exc, section_prefix=section, resolved=resolved) from exc

    models: dict[str, ModelEntry] = {}
    for alias, data in raw["models"].items():
        if isinstance(data, dict):
            models[alias] = build(ModelEntry, f"models.{alias}", {**data, "alias": alias})
    limits = {
        name: build(ProviderLimits, f"provider_limits.{name}", value or {})
        for name, value in (raw.get("provider_limits") or {}).items()
    }
    return Catalog(models=models, provider_limits=limits)


def load_catalog(path: Path | None = None) -> Catalog:
    """Load the model catalog.

    With no ``path`` the layered configuration is used (packaged defaults, then user and project
    config, environment and ``--set`` overrides). An explicit path reads that one file only.
    """
    from fusion.config.layers import resolve_config

    if path is not None:
        with path.open(encoding="utf-8") as f:
            raw: Any = yaml.safe_load(f)
        if not isinstance(raw, dict):
            msg = f"Expected a 'models' mapping in {path}"
            raise ValueError(msg)
        return catalog_from_raw(raw, str(path))
    resolved = resolve_config()
    return catalog_from_raw(resolved.data, "the resolved configuration", resolved)


def catalog_warnings(
    catalog: Catalog,
    *,
    today: date | None = None,
    stale_after_days: int = 60,
    horizon_days: int = 60,
) -> list[str]:
    """Human-readable warnings about stale, expiring or retiring catalog data."""
    day = today or date.today()
    horizon = day + timedelta(days=horizon_days)
    warnings: list[str] = []
    for alias, entry in catalog.models.items():
        if not entry.enabled or entry.provider in FREE_PROVIDERS:
            continue
        current = entry.price_at(day)
        if current is None:
            warnings.append(f"{alias}: no price schedule is in effect on {day}")
            continue
        if current.verified_on and (day - current.verified_on).days > stale_after_days:
            warnings.append(
                f"{alias}: price last verified {current.verified_on} "
                f"(older than {stale_after_days} days); re-check {current.source_url}"
            )
        if current.effective_until and current.effective_until <= horizon:
            warnings.append(
                f"{alias}: current price is valid only until {current.effective_until}; "
                "a new price applies afterwards"
            )
        if entry.retirement_not_before and entry.retirement_not_before <= horizon:
            warnings.append(
                f"{alias}: provider retirement possible from {entry.retirement_not_before} "
                f"({entry.model_id}); plan a replacement"
            )
    return warnings
