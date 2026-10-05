"""Budget levels, cost and latency tracking, and forecasts of what a run will cost."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from fusion.config.loader import BudgetConfig, ModelEntry
from fusion.telemetry.cost import PricingRegistry

LOCAL_PROVIDERS = frozenset({"ollama", "lmstudio", "mock"})


class BudgetLevel(StrEnum):
    """Routing budget presets."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    LOCAL_ONLY = "local_only"


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


# -- forecasting what a run will cost ------------------------------------------------------------
#
# Fusion cannot know a model's answer length before asking, so a forecast prices each planned call
# from assumed token counts: the prompt's own size (characters / CHARS_PER_TOKEN), a fixed
# overhead for system prompts and the response schema, and an assumed output length per kind of
# call. The assumptions are constants here so the pre-flight cap check and the generated cost
# table in docs/COSTS.md use the same arithmetic. A run's real cost always comes from the ledger.

CHARS_PER_TOKEN = 3.0  # same conservative ratio the context-window trimming uses
PROMPT_OVERHEAD_TOKENS = 900  # system prompt, claim schema and formatting around the task
ANSWER_OUTPUT_TOKENS = 1_500  # one panel or refinement answer
SYNTHESIS_OUTPUT_TOKENS = 1_500
JUDGE_OUTPUT_TOKENS = 400


def estimate_tokens(text: str) -> int:
    """Rough token count of ``text``."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


@dataclass(frozen=True)
class PlannedCall:
    """One call a run is about to make, with assumed token counts."""

    stage: str
    alias: str
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class RunForecast:
    """What planned calls would cost at the catalog prices in effect."""

    usd: float  # sum over the calls that have a price
    known: bool  # False when some call has no price (``usd`` is then a lower bound)
    unpriced: tuple[str, ...] = ()  # aliases without a price
    calls: tuple[PlannedCall, ...] = ()


def forecast_calls(
    calls: Iterable[PlannedCall],
    models: Mapping[str, ModelEntry],
    pricing: PricingRegistry,
) -> RunForecast:
    """Price planned calls from the catalog. Unknown aliases and unpriced models are reported."""
    planned = tuple(calls)
    total = 0.0
    unpriced: list[str] = []
    for call in planned:
        entry = models.get(call.alias)
        cost = None
        if entry is not None:
            cost = pricing.estimate_tokens_cost(
                provider=entry.provider,
                model_id=entry.model_id,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                pricing_alias=call.alias,
            )
        if cost is None or not cost.known or cost.amount_usd is None:
            unpriced.append(call.alias)
            continue
        total += cost.amount_usd
    return RunForecast(
        usd=total, known=not unpriced, unpriced=tuple(dict.fromkeys(unpriced)), calls=planned
    )


def price_per_million(entry: ModelEntry, pricing: PricingRegistry) -> float | None:
    """Input plus output price per 1M tokens in effect today; None when the model has no price."""
    resolved = pricing.lookup(entry.provider, entry.model_id, entry.alias or None)
    if resolved is None:
        return None
    return resolved.schedule.input_per_1m + resolved.schedule.output_per_1m
