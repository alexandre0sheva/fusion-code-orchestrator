"""Routes: a vendor model runs on its own API or through OpenRouter, as the catalog's route says."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from _provider_helpers import FakeSleep, catalog_with, make_entry
from fusion.config.catalog import load_catalog
from fusion.orchestration.context import PipelineContext
from fusion.orchestration.factory import Settings, build_pipeline, build_provider_registry
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.providers.http_utils import RetryPolicy
from fusion.providers.openrouter import OpenRouterProvider
from fusion.providers.routes import RoutedProvider, with_openrouter_routes
from fusion.routing.classifier import TaskType

VENDOR_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY")


class Spy(ModelProvider):
    """A provider that answers every call and remembers the model IDs it was asked for."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.models: list[str] = []

    def is_available(self) -> bool:
        return True

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.models.append(request.model_id)
        return ModelResponse(provider=self.name, model=request.model_id, text="ok")


@pytest.fixture
def only_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in VENDOR_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-TESTSECRET0123456789abcdef")
    monkeypatch.delenv("OLLAMA_ENABLED", raising=False)
    monkeypatch.delenv("LMSTUDIO_ENABLED", raising=False)


def test_only_an_openrouter_key_still_gives_every_vendor_a_provider(only_openrouter: None) -> None:
    providers = build_provider_registry()
    assert {"openrouter", "anthropic", "openai", "google"} <= set(providers)
    for vendor in ("anthropic", "openai", "google"):
        assert isinstance(providers[vendor], RoutedProvider)
        assert providers[vendor].is_available()
    routed = providers["anthropic"]
    assert isinstance(routed, RoutedProvider)
    served_by, model_id = routed.route_for("claude-haiku-5-5")
    assert served_by is providers["openrouter"]
    assert model_id == "anthropic/claude-haiku-5.5"


def test_with_no_key_at_all_the_error_names_openrouter_too(
    only_openrouter: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        build_provider_registry()


def test_without_an_openrouter_key_vendor_providers_are_left_alone() -> None:
    direct = Spy("anthropic")
    providers: dict[str, ModelProvider] = {"anthropic": direct}
    assert with_openrouter_routes(providers, None, load_catalog()) == {"anthropic": direct}


async def test_auto_prefers_the_vendor_api_when_its_key_is_set() -> None:
    direct, openrouter = Spy("anthropic"), Spy("openrouter")
    routed = with_openrouter_routes({"anthropic": direct}, openrouter, load_catalog())["anthropic"]
    response = await routed.complete(ModelRequest(model_id="claude-haiku-5-5", user_prompt="x"))
    assert response.provider == "anthropic"
    assert (direct.models, openrouter.models) == (["claude-haiku-5-5"], [])


async def test_auto_falls_back_to_openrouter_without_the_vendor_key() -> None:
    openrouter = Spy("openrouter")
    routed = with_openrouter_routes({}, openrouter, load_catalog())["google"]
    response = await routed.complete(ModelRequest(model_id="gemini-3.8-flash", user_prompt="x"))
    assert (response.provider, response.model) == ("openrouter", "google/gemini-3.8-flash")
    assert openrouter.models == ["google/gemini-3.8-flash"]


async def test_route_openrouter_wins_even_when_the_vendor_key_is_set() -> None:
    entry = make_entry("x", "openai", "gpt-x", route="openrouter", openrouter_id="openai/gpt-x")
    direct, openrouter = Spy("openai"), Spy("openrouter")
    routed = with_openrouter_routes({"openai": direct}, openrouter, catalog_with(entry))["openai"]
    await routed.complete(ModelRequest(model_id="gpt-x", user_prompt="x"))
    assert (direct.models, openrouter.models) == ([], ["openai/gpt-x"])


async def test_route_direct_without_the_vendor_key_is_an_auth_error_naming_it() -> None:
    entry = make_entry("x", "openai", "gpt-x", route="direct", openrouter_id="openai/gpt-x")
    openrouter = Spy("openrouter")
    routed = with_openrouter_routes({}, openrouter, catalog_with(entry))["openai"]
    response = await routed.safe_complete(ModelRequest(model_id="gpt-x", user_prompt="x"))
    assert response.error_type == "Auth"
    assert "OPENAI_API_KEY" in (response.error or "") and "route: direct" in (response.error or "")
    assert openrouter.models == []


async def test_a_model_without_an_openrouter_id_needs_its_vendor_key() -> None:
    entry = make_entry("x", "openai", "gpt-x")
    routed = with_openrouter_routes({}, Spy("openrouter"), catalog_with(entry))["openai"]
    response = await routed.safe_complete(ModelRequest(model_id="gpt-x", user_prompt="x"))
    assert response.error_type == "Auth"
    assert "OPENAI_API_KEY" in (response.error or "")


# -- Review focus 1: only OPENROUTER_API_KEY set, every default strategy runs ---------------------

ANSWER = {
    "summary": "Retry idempotent requests with capped exponential backoff.",
    "claims": [
        {"text": "Use an httpx transport with retries for connection errors", "kind": "finding"},
        {"text": "Test the retry path with httpx.MockTransport", "kind": "finding"},
    ],
    "confidence": 0.7,
}


def _openrouter_answering_everything(seen: list[dict[str, Any]]) -> OpenRouterProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(ANSWER)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 50, "cost": 0.0001},
            },
        )

    return OpenRouterProvider(
        api_key="sk-or-v1-TESTSECRET0123456789abcdef",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(sleep=FakeSleep()),
    )


def _default_strategies() -> list[str]:
    from fusion.orchestration.strategy import load_strategy_book

    book = load_strategy_book()
    names = {name for level, name in book.budget_map.items() if level != "local_only"}
    return sorted(names | {"panel-nano"})


@pytest.mark.parametrize("strategy", _default_strategies())
async def test_every_default_strategy_runs_through_openrouter_alone(
    strategy: str, tmp_path: Path
) -> None:
    seen: list[dict[str, Any]] = []
    openrouter = _openrouter_answering_everything(seen)
    providers = with_openrouter_routes({}, openrouter, load_catalog())
    pipeline = build_pipeline(Settings(use_mock=False, db_path=str(tmp_path / "r.db")), providers)
    context = PipelineContext(
        task_type=TaskType.DEFAULT,
        primary_content="How should I retry a failed HTTP call in httpx?",
        strategy=strategy,
    )
    result = await pipeline.run(context)
    assert result.final_answer
    assert result.ledger is not None and result.ledger.records
    for record in result.ledger.records:
        assert record.provider == "openrouter", record
        assert "/" in record.model_id, record  # the OpenRouter ID, e.g. anthropic/claude-haiku-5.5
        assert record.cost_usd == pytest.approx(0.0001)
        assert not record.cost_is_estimate
    assert all(p["provider"]["data_collection"] == "allow" for p in seen)
