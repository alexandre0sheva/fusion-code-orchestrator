"""Model catalog: single source of truth for model IDs and prices."""

from __future__ import annotations

import importlib
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
TODAY = date(2026, 10, 5)


def _catalog() -> Catalog:
    return load_catalog()


def test_latest_models_are_in_the_catalog() -> None:
    models = _catalog().models
    assert models["claude-opus"].model_id == "claude-opus-5-5"
    assert models["claude-sonnet"].model_id == "claude-sonnet-5-5"
    assert models["claude-haiku"].model_id == "claude-haiku-4-5-20251001"
    assert models["gpt-luna"].model_id == "gpt-6-luna"
    assert models["gpt-sol"].model_id == "gpt-6.1-sol"
    assert models["gemini-flash"].model_id == "gemini-3.8-flash"


def test_verified_list_prices() -> None:
    models = _catalog().models
    opus = models["claude-opus"].price_at(TODAY)
    assert opus is not None
    assert (opus.input_per_1m, opus.output_per_1m, opus.cached_input_per_1m) == (4.0, 20.0, 0.20)
    luna = models["gpt-luna"].price_at(TODAY)
    assert luna is not None
    assert (luna.input_per_1m, luna.output_per_1m) == (0.10, 0.50)
    sonnet = models["claude-sonnet"].price_at(TODAY)
    assert sonnet is not None
    assert (sonnet.input_per_1m, sonnet.output_per_1m) == (2.0, 10.0)


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
    assert [b.name for b in config.baselines] == ["Opus 5.5", "GPT-6.1 Sol"]
    assert config.baseline.model_id == "claude-opus-5-5"
    assert config.baselines[1].model_id == "gpt-6.1-sol"
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

    near_change = catalog_warnings(catalog, today=date(2026, 11, 20))
    assert any("gemini-flash" in w and "2026-12-31" in w for w in near_change)

    near_retirement = catalog_warnings(catalog, today=date(2026, 10, 6))
    assert any("claude-haiku" in w and "2026-10-15" in w for w in near_retirement)

    assert not [
        w for w in catalog_warnings(catalog, today=TODAY) if "verified" in w or "stale" in w
    ]


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
                        {"id": "claude-haiku-4-5-20251001"},
                    ],
                    "has_more": False,
                },
            )
        if host == "api.openai.com":
            assert request.headers["authorization"] == "Bearer ok"
            return httpx.Response(200, json={"data": [{"id": "gpt-6-luna"}]})
        if host == "generativelanguage.googleapis.com":
            assert request.headers["x-goog-api-key"] == "gk"
            assert "key=" not in str(request.url)
            return httpx.Response(
                200, json={"models": [{"name": "models/gemini-3.8-flash"}]}
            )
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
    status = {item.alias: item.status for item in report}
    assert status["claude-opus"] == "ok"
    assert status["claude-haiku"] == "ok"
    assert status["gpt-luna"] == "ok"
    assert status["gemini-flash"] == "ok"
    assert status["gpt-sol"] == "unknown_id"  # not in the mocked OpenAI list

    async with httpx.AsyncClient(transport=_transport()) as client:
        skipped = await check_catalog_live(catalog, {"anthropic": "ak"}, client)
    assert {i.alias: i.status for i in skipped}["gpt-luna"] == "skipped"


def test_models_list_command_shows_ids_and_prices() -> None:
    result = CliRunner().invoke(app, ["models", "list"])
    assert result.exit_code == 0
    for text in ("claude-opus-5-5", "gpt-6-luna", "gemini-3.8-flash"):
        assert text in result.output


def test_models_check_command_runs_offline() -> None:
    result = CliRunner().invoke(app, ["models", "check"])
    assert result.exit_code == 0
