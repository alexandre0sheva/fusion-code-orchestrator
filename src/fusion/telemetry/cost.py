"""Cost, token, and baseline comparison helpers."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from fusion.config.loader import (
    BaselineEntry,
    ModelEntry,
    PricingConfig,
    PricingEntry,
    load_baseline,
    load_pricing,
)
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


def _pricing_key(provider: str, model_id: str) -> str:
    return f"{provider}.{model_id}"


class PricingRegistry:
    """Lookup and compute costs from configured pricing entries."""

    def __init__(self, config: PricingConfig | None = None) -> None:
        self._config = config or load_pricing()

    @property
    def entries(self) -> dict[str, PricingEntry]:
        return self._config.pricing

    def get(self, provider: str, model_id: str, alias: str | None = None) -> PricingEntry | None:
        keys = []
        if alias:
            keys.append(alias)
        keys.append(_pricing_key(provider, model_id))
        for key in keys:
            entry = self._config.pricing.get(key)
            if entry:
                return entry
        return None

    def estimate_response_cost(
        self,
        response: ModelResponse,
        model_entry: ModelEntry | None = None,
    ) -> CostEstimate:
        """Compute cost for a provider response.

        Provider-returned actual cost wins. Provider-returned estimate wins next.
        Configured pricing is used when token counts are available. Legacy per-1K
        model config is the final compatibility fallback.
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

        entry = self.get(
            response.provider,
            response.model,
            f"{response.provider}.{response.model}",
        )
        if entry:
            if (
                entry.input_price_per_1m_tokens is None
                or entry.output_price_per_1m_tokens is None
            ):
                return CostEstimate(
                    None,
                    known=False,
                    is_estimate=True,
                    notes=(f"Pricing incomplete for {entry.alias}.",),
                )
            input_billable = max(input_tokens - (response.cached_input_tokens or 0), 0)
            cost = (input_billable / 1_000_000) * entry.input_price_per_1m_tokens
            cost += (output_tokens / 1_000_000) * entry.output_price_per_1m_tokens
            if response.cached_input_tokens and entry.cached_input_price_per_1m_tokens is not None:
                cost += (
                    response.cached_input_tokens / 1_000_000
                ) * entry.cached_input_price_per_1m_tokens
            if response.reasoning_tokens and entry.reasoning_price_per_1m_tokens is not None:
                cost += (
                    response.reasoning_tokens / 1_000_000
                ) * entry.reasoning_price_per_1m_tokens
            notes: tuple[str, ...] = ()
            if entry.is_estimate:
                notes = (f"Pricing for {entry.alias} is marked as an estimate.",)
            return CostEstimate(cost, known=True, is_estimate=entry.is_estimate, notes=notes)

        if model_entry and (model_entry.cost_per_1k_input or model_entry.cost_per_1k_output):
            cost = (input_tokens / 1000.0) * model_entry.cost_per_1k_input
            cost += (output_tokens / 1000.0) * model_entry.cost_per_1k_output
            return CostEstimate(
                cost,
                known=True,
                is_estimate=True,
                notes=("Used legacy model registry cost_per_1k fallback.",),
            )

        missing_key = _pricing_key(response.provider, response.model)
        return CostEstimate(
            None,
            known=False,
            is_estimate=True,
            notes=(f"No pricing configured for {missing_key}.",),
        )

    def estimate_tokens_cost(
        self,
        *,
        provider: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        pricing_alias: str | None = None,
    ) -> CostEstimate:
        entry = self.get(provider, model_id, pricing_alias)
        if entry is None:
            missing_key = pricing_alias or _pricing_key(provider, model_id)
            return CostEstimate(
                None,
                known=False,
                is_estimate=True,
                notes=(f"No pricing configured for {missing_key}.",),
            )
        if entry.input_price_per_1m_tokens is None or entry.output_price_per_1m_tokens is None:
            return CostEstimate(
                None,
                known=False,
                is_estimate=True,
                notes=(f"Pricing incomplete for {entry.alias}.",),
            )
        cost = (input_tokens / 1_000_000) * entry.input_price_per_1m_tokens
        cost += (output_tokens / 1_000_000) * entry.output_price_per_1m_tokens
        notes = (
            (f"Pricing for {entry.alias} is marked as an estimate.",)
            if entry.is_estimate
            else ()
        )
        return CostEstimate(cost, known=True, is_estimate=True, notes=notes)


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


def compute_cost(response: ModelResponse, model_entry: ModelEntry) -> float:
    """Backward-compatible numeric cost helper.

    Returns 0.0 when cost is unknown. New code should prefer PricingRegistry
    so it can preserve unknown/estimated status.
    """
    estimate = PricingRegistry().estimate_response_cost(response, model_entry)
    return estimate.amount_usd or 0.0


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
        pricing_alias=baseline_entry.pricing_alias,
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
