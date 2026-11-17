"""Model catalog: single source of truth for model IDs and prices."""

from __future__ import annotations

import importlib
import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from fusion.cli.app import app
from fusion.config.catalog import (
    Catalog,
    ModelEntry,
    PriceSchedule,
    catalog_warnings,
    load_catalog,
)
from fusion.config.catalog_check import check_catalog_live
from fusion.config.loader import load_baseline
from fusion.providers.base import ModelResponse
from fusion.telemetry.cost import PricingRegistry

CONFIG_DIR = Path(__file__).resolve().parents[1] / "src" / "fusion" / "config"
TODAY = date(2026, 11, 17)


def _catalog() -> Catalog:
    return load_catalog()


def test_latest_models_are_in_the_catalog() -> None:
    ids = {alias: entry.model_id for alias, entry in _catalog().models.items() if entry.enabled}
    assert {
        "claude-haiku": "claude-haiku-5-5",
        "claude-sonnet": "claude-sonnet-5-5",
        "claude-opus": "claude-opus-5-5",
        "claude-fable": "claude-fable-5-2",
        "gpt-luna": "gpt-6-luna",
        "gpt-terra": "gpt-6.1-terra",
        "gpt-sol": "gpt-6.2-sol",
        "gpt-astra": "gpt-6.2-astra",
        "gemini-flash": "gemini-3.8-flash",
        "gemini-hydrogen": "gemini-4-hydrogen",
        "gemini-carbon": "gemini-4-carbon",
        "gemini-argon": "gemini-4-argon",
    }.items() <= ids.items()


def test_verified_list_prices() -> None:
    models = _catalog().models

    def row(alias: str) -> tuple[float, float, float | None]:
        price = models[alias].price_at(TODAY)
        assert price is not None and price.verified_on == TODAY, alias
        return price.input_per_1m, price.output_per_1m, price.cached_input_per_1m

    assert row("claude-haiku") == (0.10, 0.50, 0.01)
    assert row("claude-sonnet") == (2.0, 10.0, 0.10)  # cached was wrongly $0.20 in 0.2.0
    assert row("claude-opus") == (4.0, 20.0, 0.20)
    assert row("claude-fable") == (8.0, 40.0, 0.20)
    assert row("gpt-luna") == (0.10, 0.50, 0.01)
    assert row("gpt-terra") == (1.0, 5.0, 0.05)
    assert row("gpt-sol") == (2.0, 10.0, 0.10)
    assert row("gpt-astra") == (8.0, 40.0, 0.40)
    assert row("gemini-hydrogen") == (1.50, 7.50, 0.075)
    assert row("gemini-carbon") == (2.0, 10.0, 0.10)


def test_older_models_are_only_disabled_legacy_entries() -> None:
    models = _catalog().models
    assert models["claude-haiku-4-5"].model_id == "claude-haiku-4-5-20251001"
    assert models["claude-haiku-4-5"].enabled is False
    assert models["gpt-sol-6-1"].enabled is False
    enabled_ids = [e.model_id for e in models.values() if e.enabled]
    assert "claude-haiku-4-5-20251001" not in enabled_ids
    assert "gpt-6.1-sol" not in enabled_ids


def test_temperature_sample_aliases_are_gone() -> None:
    assert not [alias for alias in _catalog().models if alias.endswith(("-s2", "-s3"))]


def test_no_flash_lite_model_is_in_the_catalog() -> None:
    assert not [e.model_id for e in _catalog().models.values() if "lite" in e.model_id]


def test_haiku_panel_effort_is_low() -> None:
    haiku = _catalog().models["claude-haiku"]
    assert haiku.supports_reasoning_effort and haiku.default_reasoning_effort == "low"


def test_every_cloud_price_has_provenance() -> None:
    for alias, entry in _catalog().models.items():
        if entry.provider in {"mock", "ollama", "lmstudio"}:
            continue
        assert entry.prices, f"{alias} has no prices"
        for schedule in entry.prices:
            assert schedule.verified_on is not None, alias
            assert schedule.source_url.startswith("https://"), alias


def test_price_schedule_without_provenance_is_rejected_for_cloud_models() -> None:
    with pytest.raises(ValidationError):
        ModelEntry(
            alias="x",
            provider="anthropic",
            model_id="m",
            prices=[PriceSchedule(input_per_1m=1.0, output_per_1m=2.0)],
        )


def test_local_and_mock_models_are_free_without_a_price_block() -> None:
    entry = ModelEntry(alias="m", provider="mock", model_id="m")
    schedule = entry.price_at(TODAY)
    assert schedule is not None
    assert schedule.input_per_1m == 0.0 and schedule.output_per_1m == 0.0


def test_gemini_introductory_price_changes_on_new_year() -> None:
    gemini = _catalog().models["gemini-flash"]
    last_day = gemini.price_at(date(2026, 12, 31))
    first_day = gemini.price_at(date(2027, 1, 1))
    assert last_day is not None and first_day is not None
    assert (last_day.input_per_1m, last_day.output_per_1m) == (0.75, 3.75)
    assert (first_day.input_per_1m, first_day.output_per_1m) == (1.50, 7.50)


def test_cost_uses_date_aware_price_and_bills_cached_tokens() -> None:
    registry = PricingRegistry(_catalog(), today=TODAY)
    response = ModelResponse(
        provider="anthropic",
        model="claude-opus-5-5",
        text="ok",
        input_tokens=10_000,
        output_tokens=1_000,
        cached_input_tokens=4_000,
    )
    cost = registry.estimate_response_cost(response)
    assert cost.known and not cost.is_estimate
    # 6k uncached input @ $4 + 4k cached @ $0.20 + 1k output @ $20 (per 1M)
    assert cost.amount_usd == pytest.approx(0.024 + 0.0008 + 0.020)


def test_cached_tokens_are_billed_at_input_price_when_no_cached_price() -> None:
    entry = ModelEntry(
        alias="t",
        provider="mock",
        model_id="t",
        prices=[PriceSchedule(input_per_1m=10.0, output_per_1m=20.0)],
    )
    registry = PricingRegistry(Catalog(models={"t": entry}), today=TODAY)
    response = ModelResponse(
        provider="mock",
        model="t",
        text="ok",
        input_tokens=1_000,
        output_tokens=500,
        cached_input_tokens=400,
    )
    cost = registry.estimate_response_cost(response)
    assert cost.amount_usd == pytest.approx(1_000 * 10 / 1e6 + 500 * 20 / 1e6)


def test_unknown_model_cost_stays_unknown() -> None:
    registry = PricingRegistry(Catalog(models={}), today=TODAY)
    response = ModelResponse(
        provider="x", model="y", text="ok", input_tokens=10, output_tokens=10
    )
    cost = registry.estimate_response_cost(response)
    assert cost.amount_usd is None and not cost.known


def test_baselines_resolve_from_the_catalog() -> None:
    config = load_baseline()
    assert [b.model_id for b in config.baselines] == [
        "claude-opus-5-5",
        "gpt-6.2-sol",
        "claude-fable-5-2",
        "gpt-6.2-astra",
        "gemini-4-carbon",
    ]
    assert config.baseline.model_id == "claude-opus-5-5"
    assert config.baselines[1].provider == "openai"


def test_legacy_pricing_sources_are_gone() -> None:
    assert not (CONFIG_DIR / "default_models.yaml").exists()
    assert not (CONFIG_DIR / "pricing.yaml").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("fusion.telemetry.pricing")
    assert not hasattr(importlib.import_module("fusion.config.loader"), "load_pricing")


def test_policies_only_reference_catalog_models() -> None:
    catalog = _catalog()
    policies = yaml.safe_load((CONFIG_DIR / "routing_policies.yaml").read_text())
    referenced: set[str] = set()
    for policy in policies["policies"].values():
        referenced.update(policy.get("panel_models", []))
        referenced.update(policy.get("high_risk_panel_models", []))
        referenced.update(
            filter(None, [policy.get("judge_model"), policy.get("synthesizer_model")])
        )
        for budget in policy.get("budgets", {}).values():
            referenced.update(budget.get("panel_models", []))
    assert referenced <= set(catalog.models), referenced - set(catalog.models)


def test_warnings_flag_stale_prices_and_upcoming_changes() -> None:
    catalog = _catalog()
    far_future = catalog_warnings(catalog, today=date(2027, 3, 1))
    assert any("verified" in w and "gemini-flash" in w for w in far_future)

    # Gemini 4 changes price in 14 days (flagged, with the tier change); Flash in 45 (not yet).
    now = catalog_warnings(catalog, today=TODAY)
    carbon = [w for w in now if w.startswith("gemini-carbon:") and "2026-12-01" in w]
    assert carbon and "medium -> high" in carbon[0]
    assert not [w for w in now if w.startswith("gemini-flash:")]
    later = catalog_warnings(catalog, today=date(2026, 12, 5))
    assert any(w.startswith("gemini-flash:") and "2027-01-01" in w for w in later)

    near_retirement = catalog_warnings(catalog, today=date(2027, 9, 1))
    assert any("claude-haiku" in w and "2027-10-07" in w for w in near_retirement)

    assert not [w for w in now if "verified" in w or "stale" in w or "newer" in w]


def test_warnings_flag_a_default_model_that_is_not_the_newest_in_its_family() -> None:
    catalog = _catalog()
    stale = catalog.models["claude-haiku"].model_copy(
        update={"model_id": "claude-haiku-4-5-20251001"}
    )
    current = catalog.models["claude-haiku"].model_copy(update={"enabled": False})
    models = {**catalog.models, "claude-haiku": stale, "haiku-current": current}
    warnings = catalog_warnings(Catalog(models=models), today=TODAY)
    assert any(
        w.startswith("claude-haiku:") and "claude-haiku-5-5" in w and "newer" in w
        for w in warnings
    )


def _transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.anthropic.com":
            assert request.headers["x-api-key"] == "ak"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "claude-opus-5-5"},
                        {"id": "claude-sonnet-5-5"},
                        {"id": "claude-haiku-5-5"},
                        {"id": "claude-haiku-6-0"},
                        {"id": "claude-haiku-4-5-20251001"},
                    ],
                    "has_more": False,
                },
            )
        if host == "api.openai.com":
            assert request.headers["authorization"] == "Bearer ok"
            listed = ["gpt-6-luna", "gpt-6.2-astra-law", "gpt-6.1-terra", "gpt-6.2-astra"]
            return httpx.Response(200, json={"data": [{"id": i} for i in listed]})
        if host == "generativelanguage.googleapis.com":
            assert request.headers["x-goog-api-key"] == "gk"
            assert "key=" not in str(request.url)
            listed = ["gemini-3.8-flash", "gemini-3.9-flash-lite", "gemini-4-carbon"]
            return httpx.Response(200, json={"models": [{"name": f"models/{i}"} for i in listed]})
        if host == "ai.google.dev":
            return httpx.Response(301, headers={"location": "https://aistudio.google.com/x"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_live_check_flags_unknown_ids_and_skips_providers_without_keys() -> None:
    catalog = _catalog()
    async with httpx.AsyncClient(transport=_transport()) as client:
        report = await check_catalog_live(
            catalog,
            {"anthropic": "ak", "openai": "ok", "google": "gk"},
            client,
        )
    status = {item.alias: item.status for item in report if item.status != "redirect"}
    assert status["claude-opus"] == "ok"
    assert status["gpt-luna"] == "ok"
    assert status["gemini-flash"] == "ok"  # a newer Flash-Lite is another family
    assert status["gpt-astra"] == "ok"  # gpt-6.2-astra-law is another family
    assert status["gpt-sol"] == "unknown_id"  # not in the mocked OpenAI list
    assert status["claude-haiku"] == "newer"
    newer = next(i for i in report if i.alias == "claude-haiku")
    assert "claude-haiku-6-0" in newer.detail

    async with httpx.AsyncClient(transport=_transport()) as client:
        skipped = await check_catalog_live(catalog, {"anthropic": "ak"}, client)
    assert {i.alias: i.status for i in skipped}["gpt-luna"] == "skipped"


async def test_live_check_flags_price_pages_that_redirect() -> None:
    entry = _catalog().models["gemini-flash"]
    moved = [
        p.model_copy(update={"source_url": "https://ai.google.dev/gemini-api/docs/pricing"})
        for p in entry.prices
    ]
    catalog = Catalog(models={"gemini-flash": entry.model_copy(update={"prices": moved})})
    async with httpx.AsyncClient(transport=_transport()) as client:
        report = await check_catalog_live(catalog, {}, client)
    redirects = [i for i in report if i.status == "redirect"]
    assert len(redirects) == 1
    assert "aistudio.google.com" in redirects[0].detail


def test_models_list_command_shows_ids_and_prices() -> None:
    result = CliRunner().invoke(app, ["models", "list"])
    assert result.exit_code == 0
    for text in ("claude-opus-5-5", "gpt-6-luna", "gemini-3.8-flash"):
        assert text in result.output


def test_models_list_shows_blended_price_tier_and_upcoming_tier_changes() -> None:
    result = CliRunner().invoke(app, ["models", "list", "--json", "--on", "2026-11-17"])
    assert result.exit_code == 0, result.output
    rows = {row["alias"]: row for row in json.loads(result.output)}
    assert rows["claude-sonnet"]["blended"] == pytest.approx(4.0)
    assert rows["claude-sonnet"]["tier"] == "medium"
    assert rows["claude-haiku"]["tier"] == "nano"
    assert rows["gemini-carbon"]["tier_changes"] == [{"on": "2026-12-01", "tier": "high"}]
    assert rows["gemini-flash"]["tier_changes"] == [{"on": "2027-01-01", "tier": "medium"}]
    assert rows["claude-opus"]["tier_changes"] == []

    table = CliRunner(env={"COLUMNS": "200"}).invoke(app, ["models", "list", "--on", "2026-11-17"])
    assert table.exit_code == 0
    assert "high from 2026-12-01" in table.output


def test_models_check_command_runs_offline() -> None:
    result = CliRunner().invoke(app, ["models", "check"])
    assert result.exit_code == 0
