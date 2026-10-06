"""Provider contract tests: each adapter replays recorded wire fixtures.

``tests/fixtures/providers/<provider>/*.json`` hold a provider's HTTP response (or SSE stream) and
what the adapter must make of it. Real responses carry far more than the minimal bodies the unit
tests build (ids, service tiers, safety ratings, signatures, cache breakdowns), and a provider
changing one field is how an adapter silently starts reporting zero tokens. Replaying the whole body
catches that, and ``evals/runners/record_provider_fixtures.py`` replaces a fixture with a real
recording (`uv run pytest -m live` then checks the live API still agrees).

The fixtures shipped with the repository are written from each provider's public API reference, not
recorded, and say so in their ``provenance`` field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from _provider_helpers import FakeSleep, always
from fusion.config.catalog import load_catalog
from fusion.providers.anthropic import AnthropicProvider
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.providers.google import GoogleProvider
from fusion.providers.http_utils import RetryPolicy
from fusion.providers.openai import OpenAIProvider
from fusion.security.redaction import EntropyConfig, redact_secrets

FIXTURES = Path(__file__).parent / "fixtures" / "providers"
PROVIDERS = ("anthropic", "openai", "google")
KEYS = {"anthropic": "ak-test", "openai": "sk-test", "google": "gk-test"}
BASIC = {"anthropic": "messages_cached", "openai": "chat_basic", "google": "generate_basic"}
CHECKED = (
    "text",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "finish_reason",
    "error_type",
    "retries",
)
RETRY = RetryPolicy(sleep=FakeSleep(), jitter=lambda: 0.0)


def fixture_files() -> list[Path]:
    return sorted(FIXTURES.glob("*/*.json"))


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def make_provider(name: str, transport: httpx.AsyncBaseTransport) -> ModelProvider:
    key = KEYS[name]
    if name == "anthropic":
        return AnthropicProvider(api_key=key, transport=transport, retry_policy=RETRY)
    if name == "openai":
        return OpenAIProvider(api_key=key, transport=transport, retry_policy=RETRY)
    return GoogleProvider(api_key=key, transport=transport, retry_policy=RETRY)


def http_response(spec: dict[str, Any]) -> httpx.Response:
    headers = dict(spec.get("headers", {}))
    if "sse" in spec:
        headers["content-type"] = "text/event-stream"
        return httpx.Response(spec["status"], headers=headers, content=spec["sse"].encode())
    return httpx.Response(spec["status"], headers=headers, json=spec["json"])


async def replay(path: Path) -> tuple[ModelResponse, list[httpx.Request], dict[str, Any]]:
    fixture = load(path)
    provider = path.parent.name
    entry = load_catalog().models[fixture["alias"]]
    transport, seen = always(http_response(fixture["http"]))
    spec = fixture["request"]
    request = ModelRequest(
        model_id=entry.model_id,
        user_prompt=spec["user_prompt"],
        max_tokens=spec["max_tokens"],
        stream=spec["stream"],
    )
    response = await make_provider(provider, transport).safe_complete(request)
    return response, seen, fixture


def test_every_provider_has_fixtures_for_success_failure_and_streaming() -> None:
    for provider in PROVIDERS:
        names = [p.stem for p in (FIXTURES / provider).glob("*.json")]
        assert any(n.startswith("error") for n in names), provider
        assert any(n.startswith("stream") for n in names), provider
        assert len(names) >= 6, provider


@pytest.mark.parametrize("path", fixture_files(), ids=lambda p: f"{p.parent.name}/{p.stem}")
async def test_the_adapter_reads_the_recorded_response(path: Path) -> None:
    response, _, fixture = await replay(path)
    expect = fixture["expect"]
    for name in CHECKED:
        if name in expect:
            assert getattr(response, name) == expect[name], f"{path.stem}: {name}"
    if "error_contains" in expect:
        assert response.error is not None and expect["error_contains"] in response.error
    if "error_type" not in expect:
        assert response.error is None, response.error
    assert response.provider == path.parent.name


@pytest.mark.parametrize("path", fixture_files(), ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_a_fixture_says_where_it_came_from_and_holds_no_secret(path: Path) -> None:
    fixture = load(path)
    assert fixture["provenance"] and fixture["note"] and fixture["expect"]
    # Shape rules only: an opaque base64 thinking signature is long and random but is not a key.
    found = redact_secrets(path.read_text(), entropy=EntropyConfig(enabled=False))
    assert found.redaction_count == 0, f"{path} contains {found.redacted_types}"


# --------------------------------------------------------------------------------- the request


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_the_request_goes_to_the_documented_endpoint_with_the_key_in_a_header(
    provider: str,
) -> None:
    _, seen, fixture = await replay(FIXTURES / provider / f"{BASIC[provider]}.json")
    sent = seen[0]
    entry = load_catalog().models[fixture["alias"]]
    body = json.loads(sent.content)
    prompt = fixture["request"]["user_prompt"]
    assert KEYS[provider] not in str(sent.url), "a key must never appear in a URL"
    assert "key=" not in sent.url.query.decode()
    if provider == "anthropic":
        assert (sent.url.host, sent.url.path) == ("api.anthropic.com", "/v1/messages")
        assert sent.headers["x-api-key"] == KEYS[provider]
        assert sent.headers["anthropic-version"]
        assert body["model"] == entry.model_id and body["max_tokens"] == 256
        assert prompt in json.dumps(body["messages"])
    elif provider == "openai":
        assert (sent.url.host, sent.url.path) == ("api.openai.com", "/v1/chat/completions")
        assert sent.headers["authorization"] == f"Bearer {KEYS[provider]}"
        assert body["model"] == entry.model_id
        assert prompt in json.dumps(body["messages"])
    else:
        assert sent.url.host == "generativelanguage.googleapis.com"
        assert sent.url.path == f"/v1beta/models/{entry.model_id}:generateContent"
        assert sent.headers["x-goog-api-key"] == KEYS[provider]
        assert body["generationConfig"]["maxOutputTokens"] == 256
        assert prompt in json.dumps(body["contents"])
