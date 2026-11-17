"""`fusion models check --live` against OpenRouter: prices, newer models, training providers.

Only fixture payloads (httpx.MockTransport); the real endpoints are free and need no key.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from _provider_helpers import catalog_with
from fusion.config.catalog import Catalog, ModelEntry, OpenRouterPolicy, PriceSchedule
from fusion.config.catalog_check import (
    check_openrouter_live,
    fetch_openrouter_prices,
    openrouter_family,
)

TODAY = date(2026, 11, 17)
CREATED = {"old": 1_780_000_000, "mid": 1_790_000_000, "new": 1_795_000_000}


def model(
    model_id: str,
    prompt: float,
    completion: float,
    created: str = "mid",
    overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    pricing: dict[str, Any] = {
        "prompt": f"{prompt / 1e6:.12f}",
        "completion": f"{completion / 1e6:.12f}",
    }
    if overrides:
        pricing["overrides"] = overrides
    return {"id": model_id, "created": CREATED[created], "pricing": pricing}


def entry(
    alias: str, provider: str, model_id: str, *rows: tuple[float, float, int | None], **kw: Any
) -> ModelEntry:
    prices = [
        PriceSchedule(
            input_per_1m=i,
            output_per_1m=o,
            above_prompt_tokens=above,
            verified_on=TODAY,
            source_url="https://openrouter.ai/api/v1/models",
        )
        for i, o, above in rows
    ]
    return ModelEntry(alias=alias, provider=provider, model_id=model_id, prices=prices, **kw)


def transport(
    models: list[dict[str, Any]], providers: list[dict[str, Any]] | None = None
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": models})
        if request.url.path == "/api/frontend/v1/all-providers" and providers is not None:
            return httpx.Response(200, json={"data": providers})
        return httpx.Response(404)

    return httpx.MockTransport(handler), seen


async def run(
    catalog: Catalog, models: list[dict[str, Any]], providers: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    mock, seen = transport(models, providers if providers is not None else [])
    async with httpx.AsyncClient(transport=mock) as client:
        report = await check_openrouter_live(catalog, client, today=TODAY)
    assert all("authorization" not in r.headers for r in seen)  # free endpoints, no key sent
    return {item.alias: item for item in report}


async def test_prices_come_back_per_million_tokens() -> None:
    mock, _ = transport([model("z-ai/glm-5.3", 0.0699, 1.40), model("x/y:batch", 1, 1)])
    async with httpx.AsyncClient(transport=mock) as client:
        prices = await fetch_openrouter_prices(client)
    assert prices["z-ai/glm-5.3"] == pytest.approx((0.0699, 1.40))


async def test_a_matching_price_is_ok_and_a_drift_over_5_percent_is_flagged() -> None:
    catalog = catalog_with(
        entry("glm", "openrouter", "z-ai/glm-5.3", (0.07, 1.40, None)),
        entry(
            "flash",
            "google",
            "gemini-3.8-flash",
            (0.75, 3.75, None),
            openrouter_id="google/gemini-3.8-flash",
        ),
        entry("close", "openrouter", "acme/close-1", (1.00, 2.00, None)),
    )
    report = await run(
        catalog,
        [
            model("z-ai/glm-5.3", 0.0699, 1.40),
            model("google/gemini-3.8-flash", 0.825, 4.125),
            model("acme/close-1", 1.04, 2.00),
        ],
    )
    assert report["glm"].status == "ok"
    assert report["close"].status == "ok"  # 4% is within tolerance
    assert report["flash"].status == "price_drift"
    assert report["flash"].provider == "openrouter"
    assert "10%" in report["flash"].detail and "$0.825" in report["flash"].detail


async def test_long_context_rows_are_compared_with_openrouters_overrides() -> None:
    catalog = catalog_with(
        entry("ok", "openrouter", "acme/long-1", (2.0, 6.0, None), (4.0, 12.0, 200000)),
        entry("missing", "openrouter", "acme/long-2", (2.0, 6.0, None)),
    )
    above = [{"min_prompt_tokens": 200000, "prompt": "0.000004", "completion": "0.000012"}]
    report = await run(
        catalog,
        [model("acme/long-1", 2, 6, overrides=above), model("acme/long-2", 2, 6, overrides=above)],
    )
    assert report["ok"].status == "ok"
    assert report["missing"].status == "price_drift"
    assert "200,000" in report["missing"].detail


async def test_a_time_of_day_price_matches_either_rate() -> None:
    catalog = catalog_with(
        entry("mm", "openrouter", "minimax/minimax-m3.1-flash", (0.15, 0.60, None))
    )
    peak = [
        {"utc_start": 1500, "utc_end": 100, "prompt": "0.000000075", "completion": "0.0000003"},
        {"utc_start": 100, "utc_end": 1500, "prompt": "0.00000015", "completion": "0.0000006"},
    ]
    report = await run(catalog, [model("minimax/minimax-m3.1-flash", 0.075, 0.30, overrides=peak)])
    assert report["mm"].status == "ok"


async def test_an_id_openrouter_does_not_list_is_unknown() -> None:
    catalog = catalog_with(entry("gone", "openrouter", "acme/gone-1", (1.0, 1.0, None)))
    report = await run(catalog, [model("acme/other-1", 1, 1)])
    assert report["gone"].status == "unknown_id"


async def test_a_newer_snapshot_or_version_of_the_same_family_is_flagged() -> None:
    catalog = catalog_with(
        entry("ds", "openrouter", "deepseek/deepseek-v4.1-flash-1017", (0.036, 0.5, None)),
        entry("grok", "openrouter", "x-ai/grok-4.7", (2.0, 6.0, None)),
        entry("qwen", "openrouter", "qwen/qwen3.8-flash", (0.15, 0.47, None)),
    )
    report = await run(
        catalog,
        [
            model("deepseek/deepseek-v4.1-flash-1017", 0.036, 0.5, "mid"),
            model("deepseek/deepseek-v4.1-flash", 0.30, 1.2, "old"),  # older undated snapshot
            model("deepseek/deepseek-v4.2-flash", 0.04, 0.5, "new"),  # newer version
            model("deepseek/deepseek-v4-flash-vision-exp", 0.4, 1.3, "new"),  # another family
            model("x-ai/grok-4.7", 2, 6, "mid"),
            model("x-ai/grok-4.20", 1.25, 2.5, "old"),  # bigger number, but older
            model("x-ai/grok-4.7:batch", 1.6, 4.8, "new"),  # a variant, not a model
            model("qwen/qwen3.8-flash", 0.15, 0.47, "mid"),
            model("qwen/qwen3.8-omni-flash", 0.15, 0.47, "new"),  # another family
        ],
    )
    assert report["ds"].status == "newer"
    assert "deepseek/deepseek-v4.2-flash" in report["ds"].detail
    assert report["grok"].status == "ok"
    assert report["qwen"].status == "ok"


@pytest.mark.parametrize(
    ("model_id", "family"),
    [
        ("deepseek/deepseek-v4.1-flash-1017", ("deepseek/deepseek-v#-flash", (4, 1))),
        ("mistralai/mistral-large-4-0", ("mistralai/mistral-large-#-#", (4, 0))),
        ("anthropic/claude-haiku-5.5", ("anthropic/claude-haiku-#", (5, 5))),
        ("meta/muse-ember", ("meta/muse-ember", ())),
        ("qwen/qwen3.5-plus-20260420", ("qwen/qwen#-plus", (3, 5))),
        ("x-ai/grok-4.7:batch", None),
        ("~anthropic/claude-haiku-latest", None),
    ],
)
def test_openrouter_family_names_and_versions(model_id: str, family: Any) -> None:
    assert openrouter_family(model_id) == family


def _policy_catalog(ignore: list[str]) -> Catalog:
    catalog = catalog_with(entry("glm", "openrouter", "z-ai/glm-5.3", (0.0699, 1.40, None)))
    catalog.openrouter = OpenRouterPolicy(provider_ignore=ignore)
    return catalog


def _provider(slug: str, training: bool) -> dict[str, Any]:
    return {"slug": slug, "displayName": slug.title(), "dataPolicy": {"training": training}}


async def test_a_training_provider_missing_from_the_ignore_list_is_flagged() -> None:
    providers = [
        _provider("deepseek", True),
        _provider("newcomer", True),
        _provider("wafer", False),
    ]
    report = await run(
        _policy_catalog(["deepseek"]), [model("z-ai/glm-5.3", 0.0699, 1.4)], providers
    )
    policy = report["openrouter-privacy"]
    assert policy.status == "policy"
    assert "newcomer" in policy.detail and "deepseek" not in policy.detail


async def test_an_ignore_list_covering_every_training_provider_is_ok() -> None:
    providers = [_provider("deepseek", True), _provider("wafer", False)]
    report = await run(
        _policy_catalog(["deepseek"]), [model("z-ai/glm-5.3", 0.0699, 1.4)], providers
    )
    assert report["openrouter-privacy"].status == "ok"


async def test_an_unreadable_provider_list_is_an_error_not_a_pass() -> None:
    mock, _ = transport([model("z-ai/glm-5.3", 0.0699, 1.4)], providers=None)
    async with httpx.AsyncClient(transport=mock) as client:
        report = await check_openrouter_live(_policy_catalog([]), client, today=TODAY)
    policy = next(i for i in report if i.alias == "openrouter-privacy")
    assert policy.status == "error"


async def test_an_unreachable_model_list_marks_every_openrouter_model_as_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    catalog = _policy_catalog([])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await check_openrouter_live(catalog, client, today=TODAY)
    assert {i.alias: i.status for i in report} == {"glm": "error", "openrouter-privacy": "error"}


async def test_disabled_and_local_models_are_not_checked() -> None:
    catalog = catalog_with(
        entry("off", "openrouter", "acme/off-1", (1.0, 1.0, None), enabled=False),
        entry("direct-only", "openai", "gpt-x", (1.0, 1.0, None)),
    )
    report = await run(catalog, [])
    assert set(report) == {"openrouter-privacy"}


def test_models_check_live_reports_the_openrouter_rows_too(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from typer.testing import CliRunner

    from fusion.cli import models_cmd
    from fusion.cli.app import app
    from fusion.config.catalog_check import ModelCheck

    async def direct(*_: Any) -> list[ModelCheck]:
        return [ModelCheck(alias="gpt-luna", provider="openai", model_id="gpt-6-luna", status="ok")]

    async def openrouter(*_: Any, **__: Any) -> list[ModelCheck]:
        return [
            ModelCheck(
                alias="gemini-flash",
                provider="openrouter",
                model_id="google/gemini-3.8-flash",
                status="price_drift",
                detail="10% apart",
            )
        ]

    monkeypatch.setattr(models_cmd, "check_catalog_live", direct)
    monkeypatch.setattr(models_cmd, "check_openrouter_live", openrouter)
    result = CliRunner().invoke(app, ["models", "check", "--live", "--json"])
    assert result.exit_code == 0, result.output
    live = {
        (row["alias"], row["provider"]): row["status"] for row in json.loads(result.output)["live"]
    }
    assert live == {("gpt-luna", "openai"): "ok", ("gemini-flash", "openrouter"): "price_drift"}
    text = CliRunner().invoke(app, ["models", "check", "--live"])
    assert "DRIFT" in text.output and "google/gemini-3.8-flash" in text.output
