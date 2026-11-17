"""Model catalog: the single source of truth for model IDs, capabilities and prices.

Prices carry provenance (``verified_on`` + ``source_url``) and optional effective dates so a
scheduled provider price change (for example an introductory rate ending) is modeled
explicitly instead of silently going stale.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, TypeVar

import yaml
from pydantic import BaseModel, Field, PrivateAttr, ValidationError, model_validator

if TYPE_CHECKING:
    from fusion.config.layers import ResolvedConfig

_CONFIG_DIR = Path(__file__).parent
_ModelT = TypeVar("_ModelT", bound=BaseModel)

PriceTier = Literal["nano", "low", "medium", "high", "premium"]
LatencyTier = Literal["low", "medium", "high"]
ContextTier = Literal["small", "medium", "long"]
QualityTier = Literal["weak", "medium", "strong", "frontier"]

# Providers that have no per-token bill; a missing price block means "free".
FREE_PROVIDERS = frozenset({"mock", "ollama", "lmstudio"})


class TierThreshold(BaseModel):
    """Models whose blended price is below ``below`` (USD per 1M tokens) are in ``tier``."""

    tier: PriceTier
    below: float = Field(gt=0)


class Tiering(BaseModel):
    """How a price becomes a tier: the ``tiering:`` block of ``catalog.yaml``.

    ``blended = (input_weight * input + output_weight * output) / (input_weight + output_weight)``;
    the first threshold the blended price is below names the tier, otherwise ``top_tier``.
    """

    input_weight: float = Field(gt=0)
    output_weight: float = Field(ge=0)
    thresholds: list[TierThreshold] = Field(min_length=1)
    top_tier: PriceTier

    @model_validator(mode="after")
    def _ascending(self) -> Self:
        limits = [t.below for t in self.thresholds]
        if limits != sorted(limits) or len(set(limits)) != len(limits):
            msg = "tiering.thresholds must have strictly ascending 'below' values"
            raise ValueError(msg)
        return self

    def tier_for(self, blended: float) -> PriceTier:
        for threshold in self.thresholds:
            if blended < threshold.below:
                return threshold.tier
        return self.top_tier


def blended_price(input_per_1m: float, output_per_1m: float, tiering: Tiering) -> float:
    """One USD-per-1M figure for a model, weighting input and output as ``tiering`` says."""
    total = tiering.input_weight + tiering.output_weight
    return (tiering.input_weight * input_per_1m + tiering.output_weight * output_per_1m) / total


@lru_cache(maxsize=1)
def packaged_tiering() -> Tiering:
    """The ``tiering:`` block of the packaged catalog (used when a catalog sets none)."""
    with (_CONFIG_DIR / "catalog.yaml").open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Tiering.model_validate(raw["tiering"])


class PriceSchedule(BaseModel):
    """USD prices per one million tokens, valid for an optional date range.

    A row with ``above_prompt_tokens`` is a long-context price: it applies to a request whose
    prompt (all input tokens, cached ones included) is larger than that many tokens.
    """

    input_per_1m: float = Field(ge=0)
    output_per_1m: float = Field(ge=0)
    cached_input_per_1m: float | None = Field(default=None, ge=0)
    cache_write_per_1m: float | None = Field(default=None, ge=0)
    # Only set when reasoning tokens are billed separately from output tokens.
    reasoning_per_1m: float | None = Field(default=None, ge=0)
    currency: str = "USD"
    effective_from: date | None = None
    effective_until: date | None = None
    above_prompt_tokens: int | None = Field(default=None, ge=0)
    verified_on: date | None = None
    source_url: str = ""
    is_estimate: bool = False

    def applies_to(self, prompt_tokens: int) -> bool:
        return self.above_prompt_tokens is None or prompt_tokens > self.above_prompt_tokens

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
    # True only for a model known to accept temperature/top_p/top_k/seed; current models reject
    # or ignore them, so Fusion sends none unless the catalog says otherwise.
    supports_sampling_params: bool = False
    retirement_not_before: date | None = None
    prices: list[PriceSchedule] = Field(default_factory=list)
    # Set by the Catalog that holds the entry; the packaged tiering otherwise.
    _tiering: Tiering | None = PrivateAttr(default=None)

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

    def price_for(self, on: date | None = None, prompt_tokens: int = 0) -> PriceSchedule | None:
        """The price row for a request of ``prompt_tokens`` on a date, or None if none applies.

        Among the rows in effect that day, the long-context row with the highest threshold the
        prompt is above wins over the base row; ties go to the most recent ``effective_from``.
        """
        day = on or date.today()
        covering = [p for p in self.prices if p.covers(day) and p.applies_to(prompt_tokens)]
        if not covering:
            return None
        return max(
            covering,
            key=lambda p: (
                -1 if p.above_prompt_tokens is None else p.above_prompt_tokens,
                p.effective_from or date.min,
            ),
        )

    def price_at(self, on: date | None = None) -> PriceSchedule | None:
        """The base (short-prompt) price in effect on a date, or None if none applies."""
        return self.price_for(on, 0)

    @property
    def tiering(self) -> Tiering:
        return self._tiering or packaged_tiering()

    def blended_price(self, on: date | None = None, prompt_tokens: int = 0) -> float | None:
        """Blended USD per 1M tokens (see ``Tiering``), or None when no price is in effect."""
        schedule = self.price_for(on, prompt_tokens)
        if schedule is None:
            return None
        return blended_price(schedule.input_per_1m, schedule.output_per_1m, self.tiering)

    def price_tier(self, on: date | None = None, prompt_tokens: int = 0) -> PriceTier:
        """The tier computed from the price in effect; a model with no price counts as the top."""
        blended = self.blended_price(on, prompt_tokens)
        if blended is None:
            return self.tiering.top_tier
        return self.tiering.tier_for(blended)


# The roadmap's name for a catalog model entry.
CatalogEntry = ModelEntry


class ProviderLimits(BaseModel):
    """Client-side limits for one provider; ``None`` means unlimited."""

    max_concurrent: int | None = Field(default=None, ge=1)
    rpm: int | None = Field(default=None, ge=1)


class Catalog(BaseModel):
    """All configured models keyed by alias, plus per-provider rate limits."""

    models: dict[str, ModelEntry] = Field(default_factory=dict)
    provider_limits: dict[str, ProviderLimits] = Field(default_factory=dict)
    tiering: Tiering = Field(default_factory=packaged_tiering)

    @model_validator(mode="after")
    def _share_tiering(self) -> Self:
        for entry in self.models.values():
            entry._tiering = self.tiering
        return self

    def models_in_tier(
        self, tier: PriceTier, role: str | None = None, *, on: date | None = None
    ) -> list[str]:
        """Enabled, priced models whose price is in ``tier`` on a date (today by default)."""
        return [
            alias
            for alias, entry in self.models.items()
            if entry.enabled
            and entry.provider not in FREE_PROVIDERS
            and (role is None or role in entry.roles)
            and entry.price_at(on) is not None
            and entry.price_tier(on) == tier
        ]

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
    tiering = (
        build(Tiering, "tiering", raw["tiering"])
        if isinstance(raw.get("tiering"), dict)
        else packaged_tiering()
    )
    return Catalog(models=models, provider_limits=limits, tiering=tiering)


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


# Model families by provider: the name part is the family, the numbers its version. A suffix such
# as "-lite", "-tts" or "-law" makes another family, so Flash-Lite never counts as a newer Flash.
_FAMILY_PATTERNS = {
    "anthropic": re.compile(
        r"^claude-(?P<name>[a-z]+)-(?P<major>\d+)(?:-(?P<minor>\d{1,2}))?(?:-\d{8})?$"
    ),
    "openai": re.compile(
        r"^gpt-(?P<major>\d+)(?:\.(?P<minor>\d+))?-(?P<name>[a-z][a-z0-9-]*?)"
        r"(?:-\d{4}-\d{2}-\d{2})?$"
    ),
    "google": re.compile(
        r"^gemini-(?P<major>\d+)(?:\.(?P<minor>\d+))?-(?P<name>[a-z][a-z0-9-]*?)(?:-\d{3})?$"
    ),
}


def model_family(provider: str, model_id: str) -> tuple[str, tuple[int, int]] | None:
    """``(family, (major, minor))`` for a provider model ID, or None when it has no known shape."""
    pattern = _FAMILY_PATTERNS.get(provider)
    match = pattern.match(model_id) if pattern else None
    if match is None:
        return None
    family = f"{provider}:{match.group('name')}"
    return family, (int(match.group("major")), int(match.group("minor") or 0))


def newer_in_family(provider: str, model_id: str, candidates: set[str]) -> list[str]:
    """IDs among ``candidates`` of the same family as ``model_id`` and a higher version."""
    own = model_family(provider, model_id)
    if own is None:
        return []
    newer: list[tuple[tuple[int, int], str]] = []
    for candidate in candidates:
        other = model_family(provider, candidate)
        if other is not None and other[0] == own[0] and other[1] > own[1]:
            newer.append((other[1], candidate))
    return [candidate for _, candidate in sorted(newer, reverse=True)]


def _money(schedule: PriceSchedule) -> str:
    return f"${schedule.input_per_1m:g} / ${schedule.output_per_1m:g}"


def _price_change_warnings(
    alias: str, entry: ModelEntry, day: date, current: PriceSchedule, window_days: int
) -> list[str]:
    horizon = day + timedelta(days=window_days)
    starts = sorted(
        p.effective_from
        for p in entry.prices
        if p.above_prompt_tokens is None and p.effective_from and day < p.effective_from <= horizon
    )
    warnings: list[str] = []
    for start in starts:
        before, after = entry.price_at(start - timedelta(days=1)), entry.price_at(start)
        if after is None:
            continue
        text = f"{alias}: price changes on {start}: "
        text += f"{_money(before)} -> {_money(after)}" if before else _money(after)
        tier_before = entry.price_tier(start - timedelta(days=1))
        tier_after = entry.price_tier(start)
        if tier_before != tier_after:
            text += f" (tier {tier_before} -> {tier_after})"
        warnings.append(text)
    end = current.effective_until
    if end and end <= horizon and entry.price_at(end + timedelta(days=1)) is None:
        warnings.append(
            f"{alias}: current price is valid only until {end} and no later price is known"
        )
    return warnings


def catalog_warnings(
    catalog: Catalog,
    *,
    today: date | None = None,
    stale_after_days: int = 60,
    horizon_days: int = 60,
    price_change_days: int = 30,
) -> list[str]:
    """Human-readable warnings about stale, changing, outdated or retiring catalog data."""
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
        warnings.extend(_price_change_warnings(alias, entry, day, current, price_change_days))
        known = {e.model_id for e in catalog.models.values() if e.provider == entry.provider}
        newer = newer_in_family(entry.provider, entry.model_id, known)
        if newer:
            warnings.append(
                f"{alias}: {entry.model_id} is not the newest model of its family; "
                f"the catalog has {newer[0]} (newer)"
            )
        if entry.retirement_not_before and entry.retirement_not_before <= horizon:
            warnings.append(
                f"{alias}: provider retirement possible from {entry.retirement_not_before} "
                f"({entry.model_id}); plan a replacement"
            )
    return warnings
