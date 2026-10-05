"""Cost, token, and baseline comparison helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

from fusion.config.catalog import Catalog, ModelEntry, PriceSchedule, load_catalog
from fusion.config.loader import BaselineEntry, load_baseline
from fusion.providers.base import ModelResponse


class ModelUsage(BaseModel):
    """Usage and outcome for one model call."""

    provider: str
    model_alias: str | None = None
    provider_model_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    estimated_cost_usd: float | None = None
    actual_cost_usd: float | None = None
    cost_is_estimate: bool = True
    cost_known: bool = False
    latency_ms: int = 0
    success: bool = True
    error_type: str | None = None
    error: str | None = None


class UsageSummary(BaseModel):
    """Aggregate token and latency summary for a Fusion run."""

    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    total_tokens: int | None = None
    per_model: list[ModelUsage] = Field(default_factory=list)
    fusion_wall_latency_ms: int
    panel_wall_latency_ms: int | None = None
    synthesis_latency_ms: int | None = None
    total_model_call_latency_ms: int | None = None
    max_panel_latency_ms: int | None = None
    successful_model_calls: int = 0
    failed_model_calls: int = 0


class CostComparison(BaseModel):
    """Fusion-vs-baseline cost comparison."""

    baseline_name: str
    baseline_model_id: str | None
    fusion_total_cost_usd: float | None
    baseline_estimated_cost_usd: float | None
    savings_usd: float | None
    savings_percent: float | None
    fusion_is_cheaper: bool | None
    fusion_cost_known: bool
    baseline_cost_known: bool
    comparison_notes: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class CostEstimate:
    """Internal cost estimate with provenance flags."""

    amount_usd: float | None
    known: bool
    is_estimate: bool
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedPrice:
    """A catalog model together with the price schedule in effect today."""

    alias: str
    schedule: PriceSchedule


def _pricing_key(provider: str, model_id: str) -> str:
    return f"{provider}.{model_id}"


def _token_cost(
    schedule: PriceSchedule,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> float:
    """Cost in USD.

    ``input_tokens`` includes cached reads and cache writes (provider adapters normalize this):
    plain input is billed at the input rate, reads at the cached rate, writes at the cache-write
    rate (falling back to the input rate when the catalog has none).
    """
    cached = min(cached_input_tokens, input_tokens)
    written = min(cache_write_tokens, input_tokens - cached)
    cached_price = (
        schedule.cached_input_per_1m
        if schedule.cached_input_per_1m is not None
        else schedule.input_per_1m
    )
    write_price = (
        schedule.cache_write_per_1m
        if schedule.cache_write_per_1m is not None
        else schedule.input_per_1m
    )
    cost = ((input_tokens - cached - written) / 1_000_000) * schedule.input_per_1m
    cost += (cached / 1_000_000) * cached_price
    cost += (written / 1_000_000) * write_price
    cost += (output_tokens / 1_000_000) * schedule.output_per_1m
    if reasoning_tokens and schedule.reasoning_per_1m is not None:
        cost += (reasoning_tokens / 1_000_000) * schedule.reasoning_per_1m
    return cost


class PricingRegistry:
    """Looks up date-aware prices from the model catalog and computes costs."""

    def __init__(self, catalog: Catalog | None = None, *, today: date | None = None) -> None:
        self._catalog = catalog or load_catalog()
        self._today = today

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    def lookup(
        self,
        provider: str,
        model_id: str,
        alias: str | None = None,
    ) -> ResolvedPrice | None:
        """Find the price in effect today for a catalog alias or a provider model ID."""
        entry = self._catalog.models.get(alias) if alias else None
        if entry is None:
            entry = self._catalog.find(provider, model_id)
        if entry is None:
            return None
        schedule = entry.price_at(self._today or date.today())
        if schedule is None:
            return None
        alias_name = entry.alias or _pricing_key(provider, model_id)
        return ResolvedPrice(alias=alias_name, schedule=schedule)

    def estimate_response_cost(
        self,
        response: ModelResponse,
        model_entry: ModelEntry | None = None,
    ) -> CostEstimate:
        """Compute cost for a provider response.

        Provider-returned actual cost wins, then a provider-returned estimate, then the
        catalog price schedule in effect today when token counts are available.
        """
        if response.actual_cost_usd is not None:
            return CostEstimate(response.actual_cost_usd, known=True, is_estimate=False)
        if response.cost_estimate_usd is not None:
            return CostEstimate(response.cost_estimate_usd, known=True, is_estimate=True)

        input_tokens = response.input_tokens
        output_tokens = response.output_tokens
        if input_tokens is None or output_tokens is None:
            return CostEstimate(
                None,
                known=False,
                is_estimate=True,
                notes=("Token usage unavailable; cost unknown.",),
            )

        resolved = self.lookup(
            response.provider,
            response.model,
            model_entry.alias if model_entry else None,
        )
        if resolved is None:
            missing_key = _pricing_key(response.provider, response.model)
            return CostEstimate(
                None,
                known=False,
                is_estimate=True,
                notes=(f"No price in effect for {missing_key}.",),
            )
        schedule = resolved.schedule
        cost = _token_cost(
            schedule,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=response.cached_input_tokens or 0,
            cache_write_tokens=response.cache_write_tokens or 0,
            reasoning_tokens=response.reasoning_tokens or 0,
        )
        notes: tuple[str, ...] = ()
        if schedule.is_estimate:
            notes = (f"Pricing for {resolved.alias} is marked as an estimate.",)
        return CostEstimate(cost, known=True, is_estimate=schedule.is_estimate, notes=notes)

    def estimate_tokens_cost(
        self,
        *,
        provider: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        pricing_alias: str | None = None,
    ) -> CostEstimate:
        """Estimate what a token volume would cost on a model (used for baselines)."""
        resolved = self.lookup(provider, model_id, pricing_alias)
        if resolved is None:
            missing_key = pricing_alias or _pricing_key(provider, model_id)
            return CostEstimate(
                None,
                known=False,
                is_estimate=True,
                notes=(f"No price in effect for {missing_key}.",),
            )
        cost = _token_cost(
            resolved.schedule, input_tokens=input_tokens, output_tokens=output_tokens
        )
        notes = (
            (f"Pricing for {resolved.alias} is marked as an estimate.",)
            if resolved.schedule.is_estimate
            else ()
        )
        return CostEstimate(cost, known=True, is_estimate=True, notes=notes)


def estimate_alias_cost(
    alias: str,
    *,
    input_tokens: int,
    output_tokens: int,
    registry: PricingRegistry | None = None,
) -> CostEstimate:
    """Estimate the cost of a token volume on a catalog model alias."""
    pricing = registry or PricingRegistry()
    entry = pricing.catalog.get(alias)
    return pricing.estimate_tokens_cost(
        provider=entry.provider,
        model_id=entry.model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        pricing_alias=alias,
    )


def model_usage_from_response(
    response: ModelResponse,
    *,
    model_alias: str | None = None,
    cost: CostEstimate | None = None,
) -> ModelUsage:
    """Build public usage data for a provider response."""
    total_tokens: int | None = None
    if response.input_tokens is not None or response.output_tokens is not None:
        total_tokens = (response.input_tokens or 0) + (response.output_tokens or 0)
    return ModelUsage(
        provider=response.provider,
        model_alias=model_alias or response.model_alias,
        provider_model_id=response.model,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=response.cached_input_tokens,
        cache_write_tokens=response.cache_write_tokens,
        reasoning_tokens=response.reasoning_tokens,
        estimated_cost_usd=cost.amount_usd if cost else response.cost_estimate_usd,
        actual_cost_usd=response.actual_cost_usd,
        cost_is_estimate=cost.is_estimate if cost else response.actual_cost_usd is None,
        cost_known=cost.known if cost else response.cost_estimate_usd is not None,
        latency_ms=round(response.latency_ms),
        success=response.ok,
        error_type=response.error_type,
        error=response.error,
    )


def compare_to_baseline(
    *,
    usage: UsageSummary,
    fusion_total_cost_usd: float | None,
    fusion_cost_known: bool,
    pricing: PricingRegistry | None = None,
    baseline: BaselineEntry | None = None,
) -> CostComparison:
    """Estimate what the same token volume would cost on the baseline model."""
    registry = pricing or PricingRegistry()
    baseline_entry = baseline or load_baseline().baseline
    notes: list[str] = []

    if not baseline_entry.enabled:
        return CostComparison(
            baseline_name=baseline_entry.name,
            baseline_model_id=baseline_entry.model_id,
            fusion_total_cost_usd=fusion_total_cost_usd,
            baseline_estimated_cost_usd=None,
            savings_usd=None,
            savings_percent=None,
            fusion_is_cheaper=None,
            fusion_cost_known=fusion_cost_known,
            baseline_cost_known=False,
            comparison_notes=["Baseline comparison disabled in config."],
        )

    if usage.total_input_tokens is None or usage.total_output_tokens is None:
        return CostComparison(
            baseline_name=baseline_entry.name,
            baseline_model_id=baseline_entry.model_id,
            fusion_total_cost_usd=fusion_total_cost_usd,
            baseline_estimated_cost_usd=None,
            savings_usd=None,
            savings_percent=None,
            fusion_is_cheaper=None,
            fusion_cost_known=fusion_cost_known,
            baseline_cost_known=False,
            comparison_notes=["Token usage unavailable; baseline cost unknown."],
        )

    baseline_cost = registry.estimate_tokens_cost(
        provider=baseline_entry.provider,
        model_id=baseline_entry.model_id or "",
        pricing_alias=baseline_entry.model,
        input_tokens=usage.total_input_tokens,
        output_tokens=usage.total_output_tokens,
    )
    notes.extend(baseline_cost.notes)
    notes.append(
        "Baseline cost is estimated using the same input/output token assumptions; "
        "baseline latency is unknown unless the baseline is actually called."
    )
    notes.append(f"Baseline estimate strategy: {baseline_entry.estimate_strategy}.")
    if not fusion_cost_known:
        notes.append("Fusion cost is partially unknown because at least one model lacked pricing.")

    savings_usd: float | None = None
    savings_percent: float | None = None
    fusion_is_cheaper: bool | None = None
    if (
        fusion_total_cost_usd is not None
        and fusion_cost_known
        and baseline_cost.amount_usd is not None
        and baseline_cost.known
    ):
        savings_usd = baseline_cost.amount_usd - fusion_total_cost_usd
        savings_percent = (
            (savings_usd / baseline_cost.amount_usd) * 100
            if baseline_cost.amount_usd > 0
            else None
        )
        fusion_is_cheaper = savings_usd > 0

    return CostComparison(
        baseline_name=baseline_entry.name,
        baseline_model_id=baseline_entry.model_id,
        fusion_total_cost_usd=fusion_total_cost_usd if fusion_cost_known else None,
        baseline_estimated_cost_usd=baseline_cost.amount_usd,
        savings_usd=savings_usd,
        savings_percent=savings_percent,
        fusion_is_cheaper=fusion_is_cheaper,
        fusion_cost_known=fusion_cost_known,
        baseline_cost_known=baseline_cost.known,
        comparison_notes=notes,
    )
