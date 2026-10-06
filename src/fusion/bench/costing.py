"""What a strategy costs per item, replays included.

A call replayed from the response cache is billed at zero, because another run already paid for
it. That is the right bill and the wrong cost: two arms that make the same call (a panel's Haiku
request and a solo Haiku arm's) would otherwise look cheaper the second time. Reports therefore
count every item at its price-list cost; the spend ledger keeps counting what was billed.
"""

from __future__ import annotations

from fusion.bench.store import BenchItem
from fusion.orchestration.ledger import CallRecord
from fusion.providers.base import ModelResponse
from fusion.telemetry.cost import PricingRegistry

__all__ = ["full_cost_usd"]

_PRICES: PricingRegistry | None = None


def full_cost_usd(item: BenchItem) -> float:
    """What the item's strategy costs at the price list: the billed cost plus the price of every
    call replayed from the cache. Items stored before the ledger recorded list prices are priced
    from their call records' tokens."""
    metrics = item.metrics
    if metrics.replayed_cost_usd or not metrics.cache_hits:
        return metrics.cost_usd + metrics.replayed_cost_usd
    return metrics.cost_usd + sum(_legacy_list_cost(c) for c in item.calls if c.cache_hit)


def _legacy_list_cost(call: CallRecord) -> float:
    """Price a replayed call from its tokens (catalog prices of today)."""
    global _PRICES  # noqa: PLW0603 — one registry, built on first use
    if call.list_cost_usd is not None:
        return call.list_cost_usd
    if call.input_tokens is None or call.output_tokens is None:
        return 0.0
    _PRICES = _PRICES or PricingRegistry()
    estimate = _PRICES.estimate_response_cost(
        ModelResponse(
            provider=call.provider,
            model=call.model_id,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            cached_input_tokens=call.cached_tokens,
            cache_write_tokens=call.cache_write_tokens,
            reasoning_tokens=call.reasoning_tokens,
        )
    )
    return float(estimate.amount_usd or 0.0)
