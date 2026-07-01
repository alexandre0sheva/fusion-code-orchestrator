"""Estimate external model costs for A/B comparisons."""

from __future__ import annotations

# USD per 1K tokens — keep in sync with default_models.yaml list prices
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4": {"input": 0.015, "output": 0.075},
    "claude-opus-4-8": {"input": 0.015, "output": 0.075},
    "claude-opus-4-20250514": {"input": 0.015, "output": 0.075},
    "claude-sonnet-4": {"input": 0.003, "output": 0.015},
    "claude-sonnet-4-20250514": {"input": 0.003, "output": 0.015},
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gemini-2.0-flash": {"input": 0.0001, "output": 0.0004},
}


def estimate_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    model: str = "claude-opus-4-8",
) -> float:
    """Estimate USD cost from token counts and published list pricing."""
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        pricing = MODEL_PRICING["claude-opus-4-8"]
    input_cost = (input_tokens / 1000.0) * pricing["input"]
    output_cost = (output_tokens / 1000.0) * pricing["output"]
    return input_cost + output_cost
