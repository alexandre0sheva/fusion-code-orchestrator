"""OpenRouter in the catalog: routes, OpenRouter IDs, open-weight entries and the privacy block."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from fusion.config.catalog import Catalog, ModelEntry, PriceSchedule, catalog_from_raw, load_catalog

TODAY = date(2026, 11, 17)

# alias -> (OpenRouter ID, input, output per 1M, tier), verified on OpenRouter's /models 2026-11-17.
OPEN_WEIGHT: dict[str, tuple[str, float, float, str]] = {
    "deepseek-flash": ("deepseek/deepseek-v4.1-flash-1017", 0.036, 0.50, "nano"),
    # Peak rate (01:00-15:00 UTC); off-peak is half. The catalog keeps the higher one.
    "minimax-flash": ("minimax/minimax-m3.1-flash", 0.15, 0.60, "nano"),
    "qwen-flash": ("qwen/qwen3.8-flash", 0.15, 0.47, "nano"),
    "glm": ("z-ai/glm-5.3", 0.0699, 1.40, "nano"),
    "mimo-flash": ("xiaomi/mimo-v2.6-flash", 0.14, 0.28, "nano"),
    "hy4-flash": ("tencent/hy4-flash", 0.035, 0.1405, "nano"),
    "muse-ember": ("meta/muse-ember", 0.10, 0.50, "nano"),
    "mimo-pro": ("xiaomi/mimo-v2.6-pro", 0.435, 0.87, "low"),
    "deepseek-pro": ("deepseek/deepseek-v4-pro-0813", 0.111, 4.44, "low"),
    "mistral-large": ("mistralai/mistral-large-4-0", 0.70, 2.10, "low"),
    "muse-spark-open": ("meta/muse-spark-open", 1.25, 4.25, "low"),
    "qwen-max": ("qwen/qwen4-max", 1.425, 4.275, "low"),
    "kimi": ("moonshotai/kimi-k3.1-pro", 0.2403, 8.4023, "low"),
    "grok": ("x-ai/grok-4.7", 2.0, 6.0, "medium"),
}

VENDOR_ROUTES = {
    "claude-haiku": "anthropic/claude-haiku-5.5",
    "claude-sonnet": "anthropic/claude-sonnet-5.5",
    "claude-opus": "anthropic/claude-opus-5.5",
    "claude-fable": "anthropic/claude-fable-5.2",
    "gpt-luna": "openai/gpt-6-luna",
    "gpt-luna-security": "openai/gpt-6-luna",
    "gpt-terra": "openai/gpt-6.1-terra",
    "gpt-sol": "openai/gpt-6.2-sol",
    "gpt-astra": "openai/gpt-6.2-astra",
    "gemini-flash": "google/gemini-3.8-flash",
    "gemini-hydrogen": "google/gemini-4-hydrogen",
    "gemini-carbon": "google/gemini-4-carbon",
    "gemini-argon": "google/gemini-4-argon",
}


def _price() -> PriceSchedule:
    return PriceSchedule(
        input_per_1m=1.0,
        output_per_1m=2.0,
        verified_on=TODAY,
        source_url="https://example.test/pricing",
    )


@pytest.mark.parametrize("alias", sorted(OPEN_WEIGHT))
def test_open_weight_models_are_openrouter_panel_entries(alias: str) -> None:
    model_id, input_price, output_price, tier = OPEN_WEIGHT[alias]
    entry = load_catalog().models[alias]
    assert entry.enabled
    assert (entry.provider, entry.model_id, entry.openrouter_id) == (
        "openrouter",
        model_id,
        model_id,
    )
    assert entry.roles == ["panel"]
    price = entry.price_at(TODAY)
    assert price is not None and price.verified_on == TODAY
    assert (price.input_per_1m, price.output_per_1m) == (input_price, output_price)
    assert price.source_url.startswith("https://openrouter.ai/")
    assert entry.price_tier(TODAY) == tier


def test_grok_has_its_long_context_price_above_200k() -> None:
    grok = load_catalog().models["grok"]
    above = grok.price_for(TODAY, prompt_tokens=200_001)
    assert above is not None and (above.input_per_1m, above.output_per_1m) == (4.0, 12.0)


def test_every_enabled_vendor_model_has_an_openrouter_route() -> None:
    catalog = load_catalog()
    for alias, openrouter_id in VENDOR_ROUTES.items():
        entry = catalog.models[alias]
        assert entry.route == "auto", alias
        assert entry.openrouter_id == openrouter_id, alias
    cloud = {
        alias
        for alias, e in catalog.models.items()
        if e.enabled and e.provider in {"anthropic", "openai", "google"}
    }
    assert cloud == set(VENDOR_ROUTES)


def test_find_openrouter_resolves_vendor_and_open_weight_ids() -> None:
    catalog = load_catalog()
    haiku = catalog.find_openrouter("anthropic/claude-haiku-5.5")
    assert haiku is not None and haiku.model_id == "claude-haiku-5-5"
    glm = catalog.find_openrouter("z-ai/glm-5.3")
    assert glm is not None and glm.alias == "glm"
    assert catalog.find_openrouter("nobody/nothing") is None


def test_route_openrouter_needs_an_openrouter_id() -> None:
    with pytest.raises(ValidationError, match="openrouter_id"):
        ModelEntry(
            alias="x", provider="openai", model_id="gpt-x", route="openrouter", prices=[_price()]
        )


def test_an_unknown_route_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelEntry(alias="x", provider="openai", model_id="gpt-x", route="nope", prices=[_price()])


def test_the_privacy_block_ignores_every_provider_known_to_train() -> None:
    policy = load_catalog().openrouter
    assert {"deepseek", "nvidia", "liquid", "thinkingmachines"} <= set(policy.provider_ignore)
    assert policy.verified_on == TODAY
    assert policy.source_url.startswith("https://openrouter.ai/")


def test_a_config_layer_can_add_to_the_ignore_list_but_never_shrink_it() -> None:
    raw: dict[str, Any] = {
        "models": {},
        "openrouter": {"provider_ignore": ["somehost"]},
    }
    catalog: Catalog = catalog_from_raw(raw)
    ignored = set(catalog.openrouter.provider_ignore)
    assert "somehost" in ignored
    assert {"deepseek", "nvidia", "liquid", "thinkingmachines"} <= ignored
    emptied = catalog_from_raw({"models": {}, "openrouter": {"provider_ignore": []}})
    assert "deepseek" in emptied.openrouter.provider_ignore
