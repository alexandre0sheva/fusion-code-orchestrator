"""Cost computation for model calls."""

from __future__ import annotations

from fusion.config.loader import ModelEntry
from fusion.providers.base import ModelResponse


def compute_cost(response: ModelResponse, model_entry: ModelEntry) -> float:
    """Compute USD cost for a completion based on token usage."""
    if response.cost_estimate_usd is not None:
        return response.cost_estimate_usd
    input_tokens = response.input_tokens or 0
    output_tokens = response.output_tokens or 0
    input_cost = (input_tokens / 1000.0) * model_entry.cost_per_1k_input
    output_cost = (output_tokens / 1000.0) * model_entry.cost_per_1k_output
    return input_cost + output_cost
