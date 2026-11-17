"""OpenRouter adapter: request shape, privacy fields, billed cost, retries (httpx.MockTransport)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from _provider_helpers import (
    SCHEMA,
    FakeClock,
    FakeSleep,
    always,
    body,
    catalog_with,
    make_entry,
    sequence,
    sse,
    sse_response,
)
from fusion.config.catalog import Catalog, load_catalog
from fusion.providers.base import ModelRequest
from fusion.providers.http_utils import RetryPolicy
from fusion.providers.openrouter import OpenRouterProvider
from fusion.telemetry.cost import PricingRegistry

KEY = "sk-or-v1-TESTSECRET0123456789abcdef"


def ok(text: str = "hi", usage: dict[str, Any] | None = None, **extra: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "gen-1",
            "provider": "Wafer",
            "model": "z-ai/glm-5.3",
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0000412},
            **extra,
        },
    )


def provider(
    transport: httpx.AsyncBaseTransport,
    *,
    catalog: Catalog | None = None,
    sleep: FakeSleep | None = None,
    clock: FakeClock | None = None,
) -> OpenRouterProvider:
    return OpenRouterProvider(
        api_key=KEY,
        transport=transport,
        catalog=catalog or load_catalog(),
        retry_policy=RetryPolicy(sleep=sleep or FakeSleep(), jitter=lambda: 1.0),
        clock=clock or FakeClock(),
    )


async def test_request_goes_to_openrouter_with_a_generic_title_and_the_key() -> None:
    transport, seen = always(ok())
    await provider(transport).complete(ModelRequest(model_id="z-ai/glm-5.3", user_prompt="x"))
    (request,) = seen
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert request.headers["X-Title"] == "fusion-code-orchestrator"
    assert "HTTP-Referer" not in request.headers


async def test_payload_asks_for_billed_cost_and_enforces_the_data_policy() -> None:
    transport, seen = always(ok())
    await provider(transport).complete(ModelRequest(model_id="z-ai/glm-5.3", user_prompt="x"))
    payload = body(seen[0])
    assert payload["model"] == "z-ai/glm-5.3"
    assert payload["usage"] == {"include": True}
    routing = payload["provider"]
    assert routing["require_parameters"] is True
    assert routing["data_collection"] == "allow"
    assert {"deepseek", "nvidia", "liquid", "thinkingmachines"} <= set(routing["ignore"])
    assert "zdr" not in routing and "only" not in routing


async def test_reasoning_effort_is_sent_as_openrouters_reasoning_object() -> None:
    transport, seen = always(ok())
    await provider(transport).complete(ModelRequest(model_id="z-ai/glm-5.3", user_prompt="x"))
    payload = body(seen[0])
    assert payload["reasoning"] == {"effort": "low"}  # the catalog's panel default
    assert "reasoning_effort" not in payload
    assert payload["max_tokens"] == 4096
    assert "max_completion_tokens" not in payload


async def test_a_model_without_effort_control_gets_no_reasoning_field() -> None:
    transport, seen = always(ok())
    await provider(transport).complete(ModelRequest(model_id="qwen/qwen3.8-flash", user_prompt="x"))
    assert "reasoning" not in body(seen[0])


async def test_no_sampling_parameters_are_sent() -> None:
    transport, seen = always(ok())
    request = ModelRequest(model_id="z-ai/glm-5.3", user_prompt="x", temperature=0.0, seed=7)
    await provider(transport).complete(request)
    payload = body(seen[0])
    assert "temperature" not in payload and "seed" not in payload


async def test_structured_outputs_use_json_schema_when_the_model_supports_them() -> None:
    transport, seen = always(ok('{"summary": "s"}'))
    request = ModelRequest(model_id="z-ai/glm-5.3", user_prompt="x", response_schema=SCHEMA)
    response = await provider(transport).complete(request)
    assert body(seen[0])["response_format"]["type"] == "json_schema"
    assert response.parsed_json == {"summary": "s"}


async def test_structured_outputs_fall_back_to_the_prompt_without_support() -> None:
    entry = make_entry("plain", "openrouter", "acme/plain-1", supports_json_schema=False)
    transport, seen = always(ok('{"summary": "s"}'))
    request = ModelRequest(model_id="acme/plain-1", user_prompt="x", response_schema=SCHEMA)
    await provider(transport, catalog=catalog_with(entry)).complete(request)
    payload = body(seen[0])
    assert "response_format" not in payload
    assert "JSON Schema" in payload["messages"][0]["content"]


async def test_a_vendor_model_is_found_by_its_openrouter_id() -> None:
    transport, seen = always(ok())
    request = ModelRequest(model_id="anthropic/claude-haiku-5.5", user_prompt="x")
    await provider(transport).complete(request)
    payload = body(seen[0])
    assert payload["reasoning"] == {"effort": "low"}  # claude-haiku's panel default
    assert payload["max_tokens"] == 4096


async def test_the_billed_cost_comes_from_usage_cost() -> None:
    transport, _ = always(ok())
    response = await provider(transport).complete(
        ModelRequest(model_id="z-ai/glm-5.3", user_prompt="x")
    )
    assert response.provider == "openrouter"
    assert response.actual_cost_usd == pytest.approx(0.0000412)
    assert (response.input_tokens, response.output_tokens) == (100, 20)
    cost = PricingRegistry().estimate_response_cost(response, load_catalog().models["glm"])
    assert cost.amount_usd == pytest.approx(0.0000412)
    assert not cost.is_estimate


async def test_without_usage_cost_the_catalog_estimate_is_used() -> None:
    transport, _ = always(ok(usage={"prompt_tokens": 1_000_000, "completion_tokens": 0}))
    response = await provider(transport).complete(
        ModelRequest(model_id="z-ai/glm-5.3", user_prompt="x")
    )
    assert response.actual_cost_usd is None
    cost = PricingRegistry().estimate_response_cost(response, load_catalog().models["glm"])
    assert cost.amount_usd == pytest.approx(0.0699)


async def test_429_with_retry_after_is_retried() -> None:
    sleep = FakeSleep()
    transport, seen = sequence(httpx.Response(429, headers={"Retry-After": "3"}), ok())
    response = await provider(transport, sleep=sleep).safe_complete(
        ModelRequest(model_id="z-ai/glm-5.3", user_prompt="x")
    )
    assert response.ok
    assert sleep.calls == [3.0]
    assert response.retries == 1
    assert len(seen) == 2


async def test_an_error_inside_a_200_body_is_an_error() -> None:
    transport, _ = always(
        httpx.Response(200, json={"error": {"message": "upstream failed", "code": 502}})
    )
    response = await provider(transport).safe_complete(
        ModelRequest(model_id="z-ai/glm-5.3", user_prompt="x")
    )
    assert not response.ok
    assert "upstream failed" in (response.error or "")


async def test_without_a_key_the_call_fails_with_an_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    transport, seen = always(ok())
    adapter = OpenRouterProvider(transport=transport)
    assert not adapter.is_available()
    response = await adapter.safe_complete(ModelRequest(model_id="z-ai/glm-5.3", user_prompt="x"))
    assert response.error_type == "Auth"
    assert "OPENROUTER_API_KEY" in (response.error or "")
    assert seen == []


def test_the_key_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    assert OpenRouterProvider().is_available()


async def test_the_key_never_appears_in_error_text() -> None:
    transport, _ = always(httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}}))
    response = await provider(transport).safe_complete(
        ModelRequest(model_id="z-ai/glm-5.3", user_prompt="x")
    )
    assert KEY not in (response.error or "")


async def test_a_streamed_call_reads_the_billed_cost_from_the_last_chunk() -> None:
    clock = FakeClock()
    stream = sse(
        (None, {"choices": [{"delta": {"content": "he"}}]}),
        (None, {"choices": [{"delta": {"content": "llo"}, "finish_reason": "stop"}]}),
        (
            None,
            {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2, "cost": 1e-5}},
        ),
        (None, "[DONE]"),
    )
    transport, seen = always(sse_response(clock, [(0.1, stream)]))
    request = ModelRequest(model_id="z-ai/glm-5.3", user_prompt="x", stream=True)
    response = await provider(transport, clock=clock).complete(request)
    assert response.text == "hello"
    assert response.actual_cost_usd == pytest.approx(1e-5)
    assert body(seen[0])["usage"] == {"include": True}
