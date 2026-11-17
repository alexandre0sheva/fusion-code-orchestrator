"""Price-derived tiers and long-context prices (roadmap 0.3.0, Task 3)."""

from __future__ import annotations

from datetime import date

import pytest

from fusion.config.catalog import (
    Catalog,
    ModelEntry,
    PriceSchedule,
    Tiering,
    blended_price,
    load_catalog,
)
from fusion.orchestration.budget_guard import preflight
from fusion.orchestration.strategy import Strategy
from fusion.providers.base import ModelResponse
from fusion.telemetry.cost import PricingRegistry

TODAY = date(2026, 11, 17)
SOURCE = "https://example.com/pricing"


def _entry(
    input_per_1m: float, output_per_1m: float, *rows: PriceSchedule, alias: str = "m"
) -> ModelEntry:
    base = PriceSchedule(
        input_per_1m=input_per_1m,
        output_per_1m=output_per_1m,
        verified_on=TODAY,
        source_url=SOURCE,
    )
    return ModelEntry(alias=alias, provider="openai", model_id=alias, prices=[base, *rows])


def _long_row(threshold: int, input_per_1m: float, output_per_1m: float) -> PriceSchedule:
    return PriceSchedule(
        input_per_1m=input_per_1m,
        output_per_1m=output_per_1m,
        above_prompt_tokens=threshold,
        verified_on=TODAY,
        source_url=SOURCE,
    )


# ------------------------------------------------------------------------------ tier function


def test_blended_price_weights_input_three_to_one() -> None:
    tiering = load_catalog().tiering
    assert blended_price(2.0, 10.0, tiering) == pytest.approx(4.0)
    assert blended_price(0.10, 0.50, tiering) == pytest.approx(0.20)


@pytest.mark.parametrize(
    ("input_per_1m", "output_per_1m", "tier"),
    [
        (0.10, 0.50, "nano"),  # Haiku 5.5, Luna: $0.20
        (1.0, 5.0, "low"),  # Terra: $2.00
        (0.75, 3.75, "low"),  # Gemini 3.8 Flash until 2026-12-31: $1.50
        (1.5, 7.5, "medium"),  # Hydrogen: $3.00
        (2.0, 10.0, "medium"),  # Sonnet 5.5, Sol: $4.00 (the owner's "$2/$10 = middle")
        (4.0, 20.0, "high"),  # Opus 5.5: $8.00
        (8.0, 40.0, "premium"),  # Fable 5.2, GPT-6.2 Astra: $16.00
    ],
)
def test_tier_follows_the_blended_price(
    input_per_1m: float, output_per_1m: float, tier: str
) -> None:
    entry = _entry(input_per_1m, output_per_1m)
    assert Catalog(models={"m": entry}).models["m"].price_tier(TODAY) == tier


@pytest.mark.parametrize(
    ("blended", "tier"),
    [
        (0.499, "nano"),
        (0.50, "low"),
        (2.499, "low"),
        (2.50, "medium"),
        (5.999, "medium"),
        (6.0, "high"),
        (11.999, "high"),
        (12.0, "premium"),
    ],
)
def test_tier_thresholds_are_exclusive_upper_bounds(blended: float, tier: str) -> None:
    # input == output == blended makes the blended price exactly ``blended``.
    entry = _entry(blended, blended)
    assert entry.price_tier(TODAY) == tier


def test_free_models_are_nano() -> None:
    assert ModelEntry(alias="m", provider="mock", model_id="m").price_tier() == "nano"


def test_tiering_comes_from_the_catalog_not_from_code() -> None:
    tiering = load_catalog().tiering
    assert (tiering.input_weight, tiering.output_weight) == (3, 1)
    assert [(t.tier, t.below) for t in tiering.thresholds] == [
        ("nano", 0.5),
        ("low", 2.5),
        ("medium", 6.0),
        ("high", 12.0),
    ]
    assert tiering.top_tier == "premium"
    # A catalog with other thresholds puts the same model in another tier.
    strict = Tiering.model_validate(
        {
            "input_weight": 1,
            "output_weight": 1,
            "thresholds": [{"tier": "nano", "below": 0.1}],
            "top_tier": "premium",
        }
    )
    catalog = Catalog(models={"m": _entry(0.10, 0.50)}, tiering=strict)
    assert catalog.models["m"].price_tier(TODAY) == "premium"


def test_cost_tier_is_no_longer_a_catalog_field() -> None:
    assert "cost_tier" not in ModelEntry.model_fields


# ------------------------------------------------------------------------ price schedules


def test_tier_changes_on_the_date_a_price_schedule_changes() -> None:
    models = load_catalog().models
    carbon = models["gemini-carbon"]
    assert carbon.price_tier(date(2026, 11, 30)) == "medium"
    assert carbon.price_tier(date(2026, 12, 1)) == "high"
    flash = models["gemini-flash"]
    assert flash.price_tier(date(2026, 12, 31)) == "low"
    assert flash.price_tier(date(2027, 1, 1)) == "medium"


def test_price_for_picks_the_long_context_row_above_its_threshold() -> None:
    entry = _entry(0.10, 0.50, _long_row(100_000, 0.50, 2.50))
    at_threshold = entry.price_for(TODAY, 100_000)
    above = entry.price_for(TODAY, 100_001)
    assert at_threshold is not None and above is not None
    assert at_threshold.input_per_1m == 0.10
    assert (above.input_per_1m, above.output_per_1m) == (0.50, 2.50)
    assert entry.price_at(TODAY) == entry.price_for(TODAY, 0)


def test_price_for_respects_dates_of_long_context_rows() -> None:
    old_long = _long_row(100_000, 0.50, 2.50).model_copy(
        update={"effective_until": date(2026, 11, 30)}
    )
    entry = _entry(0.10, 0.50, old_long)
    later = entry.price_for(date(2026, 12, 1), 200_000)
    assert later is not None and later.input_per_1m == 0.10


def test_haiku_crosses_into_the_low_tier_above_100k_prompt_tokens() -> None:
    haiku = load_catalog().models["claude-haiku"]
    assert haiku.price_tier(TODAY, prompt_tokens=100_000) == "nano"
    assert haiku.price_tier(TODAY, prompt_tokens=100_001) == "low"


def test_openai_long_context_rows_start_above_272k() -> None:
    luna = load_catalog().models["gpt-luna"]
    short = luna.price_for(TODAY, 272_000)
    long = luna.price_for(TODAY, 272_001)
    assert short is not None and long is not None
    assert (short.input_per_1m, short.output_per_1m) == (0.10, 0.50)
    assert (long.input_per_1m, long.output_per_1m) == (0.20, 0.75)


# ------------------------------------------------------------------------- tier listing


def test_models_in_tier_lists_enabled_models_of_that_tier() -> None:
    catalog = load_catalog()
    nano = catalog.models_in_tier("nano", on=TODAY)
    assert {"claude-haiku", "gpt-luna"} <= set(nano)
    assert all(catalog.models[a].enabled for a in nano)
    assert all(catalog.models[a].provider != "mock" for a in nano)
    assert catalog.models_in_tier("nano", role="judge", on=TODAY) == ["gpt-luna"]
    assert "claude-opus" in catalog.models_in_tier("high", on=TODAY)
    assert "claude-fable" in catalog.models_in_tier("premium", on=TODAY)


# --------------------------------------------------------------- ledger and budget guard


def _haiku_response(input_tokens: int) -> ModelResponse:
    return ModelResponse(
        provider="anthropic",
        model="claude-haiku-5-5",
        text="ok",
        input_tokens=input_tokens,
        output_tokens=1_000_000,
        latency_ms=1.0,
    )


def test_ledger_prices_a_call_by_its_actual_prompt_size() -> None:
    pricing = PricingRegistry(load_catalog(), today=TODAY)
    short = pricing.estimate_response_cost(_haiku_response(100_000))
    long = pricing.estimate_response_cost(_haiku_response(200_000))
    assert short.amount_usd == pytest.approx(0.1 * 0.10 + 0.50)
    assert long.amount_usd == pytest.approx(0.2 * 0.50 + 2.50)


def test_max_cost_guard_uses_the_long_context_price() -> None:
    """Review focus 3: a prompt over Haiku 5.5's 100K threshold is forecast at the higher price."""
    catalog = load_catalog()
    pricing = PricingRegistry(catalog, today=TODAY)
    solo = Strategy(
        name="solo-haiku-capped",
        kind="solo",
        members=[{"model": "claude-haiku"}],
        max_cost_usd=0.03,
    )

    def check(prompt_tokens: int) -> bool | None:
        return preflight(
            solo,
            models=catalog.models,
            pricing=pricing,
            judge_model="gpt-luna",
            prompt_tokens=prompt_tokens,
        ).within_cap

    # The forecast adds 900 tokens of overhead and 1,500 output tokens to the prompt.
    # 90.9K tokens at $0.10 / $0.50 is about $0.010: under the cap ...
    assert check(90_000) is True
    # ... 100.9K tokens crosses the 100K threshold: about $0.054 at $0.50 / $2.50, refused.
    assert check(100_000) is False
