"""Per-provider contract tests: payload mapping and token-accounting normalization."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from _provider_helpers import (
    PNG_BYTES,
    SCHEMA,
    FakeSleep,
    always,
    anthropic_ok,
    body,
    catalog_with,
    google_ok,
    make_entry,
    openai_ok,
)
from fusion.config.catalog import load_catalog
from fusion.providers.anthropic import AnthropicProvider
from fusion.providers.base import ImagePart, Message, ModelRequest
from fusion.providers.google import GoogleProvider
from fusion.providers.http_utils import RetryPolicy
from fusion.providers.lmstudio import LMStudioProvider
from fusion.providers.ollama import OllamaProvider
from fusion.providers.openai import OpenAIProvider
from fusion.telemetry.cost import PricingRegistry

RETRY = RetryPolicy(sleep=FakeSleep(), jitter=lambda: 0.0)
IMAGE = ImagePart(media_type="image/png", data=PNG_BYTES)
IMAGE_B64 = base64.b64encode(PNG_BYTES).decode()
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-4-5-20251001"


def anthropic(transport: httpx.AsyncBaseTransport, catalog: Any = None) -> AnthropicProvider:
    return AnthropicProvider(
        api_key="ak-test", transport=transport, retry_policy=RETRY, catalog=catalog
    )


def openai(transport: httpx.AsyncBaseTransport, catalog: Any = None) -> OpenAIProvider:
    return OpenAIProvider(
        api_key="sk-test", transport=transport, retry_policy=RETRY, catalog=catalog
    )


def google(transport: httpx.AsyncBaseTransport, catalog: Any = None) -> GoogleProvider:
    return GoogleProvider(
        api_key="gk-test", transport=transport, retry_policy=RETRY, catalog=catalog
    )


# --------------------------------------------------------------------------------- token accounting


async def test_anthropic_input_tokens_include_cache_reads_and_writes() -> None:
    usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": 80,
        "cache_creation_input_tokens": 5,
        "output_tokens": 20,
    }
    transport, _ = always(anthropic_ok(usage=usage))
    response = await anthropic(transport).safe_complete(
        ModelRequest(model_id=HAIKU, user_prompt="hi")
    )
    assert response.input_tokens == 95
    assert response.cached_input_tokens == 80
    assert response.cache_write_tokens == 5
    assert response.output_tokens == 20


async def test_anthropic_cache_write_tokens_are_priced_from_the_catalog() -> None:
    usage = {
        "input_tokens": 500,
        "cache_read_input_tokens": 400,
        "cache_creation_input_tokens": 100,
        "output_tokens": 200,
    }
    transport, _ = always(anthropic_ok(usage=usage))
    response = await anthropic(transport).safe_complete(
        ModelRequest(model_id=HAIKU, user_prompt="hi")
    )
    cost = PricingRegistry().estimate_response_cost(response)
    # haiku: $1.00 in, $0.10 cached read, $1.25 cache write, $5.00 out (per 1M)
    expected = (500 * 1.0 + 400 * 0.10 + 100 * 1.25 + 200 * 5.0) / 1_000_000
    assert cost.amount_usd == pytest.approx(expected)


async def test_openai_reasoning_tokens_stay_inside_output_tokens() -> None:
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 40},
        "completion_tokens_details": {"reasoning_tokens": 30},
    }
    transport, _ = always(openai_ok(usage=usage))
    response = await openai(transport).safe_complete(
        ModelRequest(model_id="gpt-6-luna", user_prompt="hi")
    )
    assert (response.input_tokens, response.output_tokens) == (100, 50)
    assert response.cached_input_tokens == 40
    assert response.reasoning_tokens == 30


async def test_google_thinking_tokens_are_added_to_output_tokens() -> None:
    usage = {
        "promptTokenCount": 100,
        "candidatesTokenCount": 30,
        "thoughtsTokenCount": 20,
        "cachedContentTokenCount": 10,
    }
    transport, _ = always(google_ok(usage=usage))
    response = await google(transport).safe_complete(
        ModelRequest(model_id="gemini-3.8-flash", user_prompt="hi")
    )
    assert response.input_tokens == 100
    assert response.output_tokens == 50
    assert response.reasoning_tokens == 20
    assert response.cached_input_tokens == 10


# --------------------------------------------------------------------------------------- security


async def test_google_key_travels_in_header_never_in_url() -> None:
    transport, seen = always(google_ok())
    await google(transport).safe_complete(
        ModelRequest(model_id="gemini-3.8-flash", user_prompt="hi")
    )
    request = seen[0]
    assert request.headers["x-goog-api-key"] == "gk-test"
    assert "gk-test" not in str(request.url)
    assert request.url.query == b""


async def test_transport_error_text_never_contains_the_api_key() -> None:
    secret = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE12345"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url} with token {secret}")

    provider = GoogleProvider(
        api_key=secret, transport=httpx.MockTransport(boom), retry_policy=RETRY
    )
    response = await provider.safe_complete(
        ModelRequest(model_id="gemini-3.8-flash", user_prompt="hi")
    )
    assert response.error is not None
    assert secret not in response.error


def test_provider_sources_never_put_keys_in_query_strings() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "fusion" / "providers"
    offenders = [p.name for p in root.glob("*.py") if "key=" in p.read_text()]
    assert offenders == []


# ------------------------------------------------------------------------------------- Anthropic


async def test_anthropic_sampling_params_follow_the_catalog_flag_not_a_regex() -> None:
    catalog = catalog_with(
        make_entry("future", "anthropic", "claude-test-1-0", supports_sampling_params=False),
        make_entry("past", "anthropic", "claude-test-0-9", supports_sampling_params=True),
    )
    transport, seen = always(anthropic_ok())
    provider = anthropic(transport, catalog)
    for model in ("claude-test-1-0", "claude-test-0-9"):
        await provider.safe_complete(ModelRequest(model_id=model, user_prompt="x", temperature=0.2))
    assert "temperature" not in body(seen[0])
    assert body(seen[1])["temperature"] == 0.2


async def test_anthropic_catalog_models_reject_sampling_for_opus_but_not_haiku() -> None:
    transport, seen = always(anthropic_ok())
    provider = anthropic(transport)
    await provider.safe_complete(ModelRequest(model_id=OPUS, user_prompt="x", temperature=0.2))
    await provider.safe_complete(ModelRequest(model_id=HAIKU, user_prompt="x", temperature=0.2))
    assert "temperature" not in body(seen[0])
    assert body(seen[1])["temperature"] == 0.2


async def test_anthropic_effort_defaults_from_catalog_and_can_be_overridden() -> None:
    entry = load_catalog().get("claude-opus")
    transport, seen = always(anthropic_ok())
    provider = anthropic(transport)
    await provider.safe_complete(ModelRequest(model_id=OPUS, user_prompt="x"))
    await provider.safe_complete(
        ModelRequest(model_id=OPUS, user_prompt="x", reasoning_effort="low")
    )
    await provider.safe_complete(ModelRequest(model_id=HAIKU, user_prompt="x"))
    assert body(seen[0])["output_config"]["effort"] == entry.default_reasoning_effort
    assert body(seen[1])["output_config"]["effort"] == "low"
    assert "output_config" not in body(seen[2])


async def test_anthropic_structured_output_uses_native_json_schema() -> None:
    transport, seen = always(anthropic_ok(text='{"summary": "ok"}'))
    response = await anthropic(transport).safe_complete(
        ModelRequest(model_id=OPUS, user_prompt="x", response_schema=SCHEMA)
    )
    fmt = body(seen[0])["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert fmt["schema"]["properties"]["summary"] == {"type": "string"}
    assert "tool_choice" not in body(seen[0])  # forced tool use is rejected by Claude 5.x
    assert "effort" in body(seen[0])["output_config"]
    assert response.parsed_json == {"summary": "ok"}


async def test_anthropic_schema_falls_back_to_prompt_and_scraping_without_native_support() -> None:
    fenced = 'Here you go:\n```json\n{"summary": "scraped"}\n```'
    transport, seen = always(anthropic_ok(text=fenced))
    response = await anthropic(transport).safe_complete(
        ModelRequest(
            model_id=HAIKU, system_prompt="Be brief.", user_prompt="x", response_schema=SCHEMA
        )
    )
    payload = body(seen[0])
    assert "output_config" not in payload
    assert "summary" in payload["system"]  # the schema is spelled out for the model
    assert "Be brief." in payload["system"]
    assert response.parsed_json == {"summary": "scraped"}


async def test_anthropic_cache_prefix_marks_system_block() -> None:
    transport, seen = always(anthropic_ok())
    await anthropic(transport).safe_complete(
        ModelRequest(
            model_id=HAIKU, system_prompt="shared rules", user_prompt="task", cache_prefix=True
        )
    )
    payload = body(seen[0])
    assert payload["system"] == [
        {"type": "text", "text": "shared rules", "cache_control": {"type": "ephemeral"}}
    ]
    assert payload["messages"] == [{"role": "user", "content": "task"}]


async def test_anthropic_cache_prefix_covers_everything_before_the_last_message() -> None:
    transport, seen = always(anthropic_ok())
    request = ModelRequest(
        model_id=HAIKU,
        system_prompt="rules",
        cache_prefix=True,
        messages=[
            Message(role="user", content="shared task"),
            Message(role="assistant", content="draft"),
            Message(role="user", content="critique"),
        ],
    )
    await anthropic(transport).safe_complete(request)
    messages = body(seen[0])["messages"]
    assert messages[1]["content"] == [
        {"type": "text", "text": "draft", "cache_control": {"type": "ephemeral"}}
    ]
    assert messages[2]["content"] == "critique"


async def test_anthropic_cache_off_by_default() -> None:
    transport, seen = always(anthropic_ok())
    await anthropic(transport).safe_complete(
        ModelRequest(model_id=HAIKU, system_prompt="rules", user_prompt="task")
    )
    assert body(seen[0])["system"] == "rules"


async def test_anthropic_thinking_budget_is_rejected_on_adaptive_models_without_a_call() -> None:
    transport, seen = always(anthropic_ok())
    response = await anthropic(transport).safe_complete(
        ModelRequest(model_id=OPUS, user_prompt="x", thinking_budget_tokens=4000)
    )
    assert response.error_type == "BadRequest"
    assert "reasoning_effort" in (response.error or "")
    assert seen == []


async def test_anthropic_thinking_budget_is_sent_for_legacy_thinking_models() -> None:
    transport, seen = always(anthropic_ok())
    await anthropic(transport).safe_complete(
        ModelRequest(model_id=HAIKU, user_prompt="x", thinking_budget_tokens=4000, temperature=0.3)
    )
    payload = body(seen[0])
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 4000}
    assert "temperature" not in payload  # sampling params conflict with extended thinking


async def test_anthropic_images_become_base64_blocks_before_the_text() -> None:
    transport, seen = always(anthropic_ok())
    await anthropic(transport).safe_complete(
        ModelRequest(model_id=OPUS, user_prompt="what is this?", images=[IMAGE])
    )
    content = body(seen[0])["messages"][-1]["content"]
    assert content[0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": IMAGE_B64},
    }
    assert content[-1] == {"type": "text", "text": "what is this?"}


async def test_images_are_refused_for_models_the_catalog_marks_text_only() -> None:
    catalog = catalog_with(make_entry("blind", "anthropic", "claude-blind", supports_vision=False))
    transport, seen = always(anthropic_ok())
    response = await anthropic(transport, catalog).safe_complete(
        ModelRequest(model_id="claude-blind", user_prompt="x", images=[IMAGE])
    )
    assert response.error_type == "BadRequest"
    assert "vision" in (response.error or "").lower()
    assert seen == []


# --------------------------------------------------------------------------------------- OpenAI


async def test_openai_reasoning_models_use_max_completion_tokens_and_effort() -> None:
    entry = load_catalog().get("gpt-luna")
    transport, seen = always(openai_ok())
    provider = openai(transport)
    await provider.safe_complete(
        ModelRequest(model_id="gpt-6-luna", user_prompt="x", max_tokens=777, temperature=0.4)
    )
    await provider.safe_complete(
        ModelRequest(model_id="gpt-6-luna", user_prompt="x", reasoning_effort="high")
    )
    first, second = body(seen[0]), body(seen[1])
    assert first["max_completion_tokens"] == 777
    assert "max_tokens" not in first
    assert "temperature" not in first  # catalog: sampling params unsupported
    assert first["reasoning_effort"] == entry.default_reasoning_effort
    assert second["reasoning_effort"] == "high"


async def test_openai_models_without_reasoning_keep_legacy_parameters() -> None:
    catalog = catalog_with(make_entry("old", "openai", "gpt-old", supports_reasoning_effort=False))
    transport, seen = always(openai_ok())
    await openai(transport, catalog).safe_complete(
        ModelRequest(model_id="gpt-old", user_prompt="x", max_tokens=99, temperature=0.4)
    )
    payload = body(seen[0])
    assert payload["max_tokens"] == 99
    assert payload["temperature"] == 0.4
    assert "reasoning_effort" not in payload


async def test_openai_structured_output_sends_json_schema() -> None:
    transport, seen = always(openai_ok(text='{"summary": "ok"}'))
    response = await openai(transport).safe_complete(
        ModelRequest(model_id="gpt-6-luna", user_prompt="x", response_schema=SCHEMA)
    )
    fmt = body(seen[0])["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] == SCHEMA
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["name"]
    assert response.parsed_json == {"summary": "ok"}


async def test_openai_schema_falls_back_to_json_object_plus_instruction() -> None:
    catalog = catalog_with(
        make_entry("plain", "openai", "gpt-plain", supports_json=True, supports_json_schema=False)
    )
    transport, seen = always(openai_ok(text='noise {"summary": "x"} noise'))
    response = await openai(transport, catalog).safe_complete(
        ModelRequest(model_id="gpt-plain", user_prompt="x", response_schema=SCHEMA)
    )
    payload = body(seen[0])
    assert payload["response_format"] == {"type": "json_object"}
    assert "summary" in payload["messages"][0]["content"]
    assert response.parsed_json == {"summary": "x"}


async def test_openai_images_become_data_uri_parts() -> None:
    transport, seen = always(openai_ok())
    await openai(transport).safe_complete(
        ModelRequest(model_id="gpt-6-luna", user_prompt="look", images=[IMAGE])
    )
    content = body(seen[0])["messages"][-1]["content"]
    assert {"type": "text", "text": "look"} in content
    assert {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{IMAGE_B64}"},
    } in content


# --------------------------------------------------------------------------------------- Google


async def test_google_thinking_level_comes_from_catalog_and_is_clamped() -> None:
    entry = load_catalog().get("gemini-flash")
    transport, seen = always(google_ok())
    provider = google(transport)
    model = "gemini-3.8-flash"
    await provider.safe_complete(ModelRequest(model_id=model, user_prompt="x"))
    for effort in ("minimal", "none", "max", "medium"):
        await provider.safe_complete(
            ModelRequest(model_id=model, user_prompt="x", reasoning_effort=effort)
        )
    levels = [
        body(r).get("generationConfig", {}).get("thinkingConfig", {}).get("thinkingLevel")
        for r in seen
    ]
    assert levels[0] == entry.default_reasoning_effort
    assert levels[1:] == ["low", "low", "high", "medium"]  # `minimal` errors on 3.8 Flash


async def test_google_response_schema_is_inlined_and_sanitized() -> None:
    transport, seen = always(google_ok(text='{"summary": "ok"}'))
    response = await google(transport).safe_complete(
        ModelRequest(model_id="gemini-3.8-flash", user_prompt="x", response_schema=SCHEMA)
    )
    config = body(seen[0])["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    schema = config["responseSchema"]
    assert "$defs" not in schema
    assert schema["properties"]["items"]["items"]["properties"]["id"] == {"type": "string"}
    assert "additionalProperties" not in str(schema)
    assert response.parsed_json == {"summary": "ok"}


async def test_google_system_prompt_uses_system_instruction() -> None:
    transport, seen = always(google_ok())
    await google(transport).safe_complete(
        ModelRequest(model_id="gemini-3.8-flash", system_prompt="be terse", user_prompt="x")
    )
    payload = body(seen[0])
    assert payload["systemInstruction"] == {"parts": [{"text": "be terse"}]}
    assert payload["contents"] == [{"role": "user", "parts": [{"text": "x"}]}]


async def test_google_images_become_inline_data() -> None:
    transport, seen = always(google_ok())
    await google(transport).safe_complete(
        ModelRequest(model_id="gemini-3.8-flash", user_prompt="look", images=[IMAGE])
    )
    parts = body(seen[0])["contents"][-1]["parts"]
    assert {"inlineData": {"mimeType": "image/png", "data": IMAGE_B64}} in parts
    assert {"text": "look"} in parts


# ------------------------------------------------------------------------------ local providers


async def test_ollama_maps_images_and_schema() -> None:
    catalog = catalog_with(
        make_entry("local", "ollama", "llama-v", supports_vision=True, supports_json_schema=True)
    )
    transport, seen = always(
        httpx.Response(200, json={"message": {"content": '{"summary": "x"}'}, "eval_count": 3})
    )
    provider = OllamaProvider(transport=transport, retry_policy=RETRY, catalog=catalog)
    response = await provider.safe_complete(
        ModelRequest(model_id="llama-v", user_prompt="x", images=[IMAGE], response_schema=SCHEMA)
    )
    payload = body(seen[0])
    assert payload["messages"][-1]["images"] == [IMAGE_B64]
    assert payload["format"] == SCHEMA
    assert response.parsed_json == {"summary": "x"}


async def test_ollama_unreachable_server_gets_a_helpful_message() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    provider = OllamaProvider(
        base_url="http://localhost:9", transport=httpx.MockTransport(refuse), retry_policy=RETRY
    )
    response = await provider.safe_complete(ModelRequest(model_id="m", user_prompt="x"))
    assert "Ollama not reachable at http://localhost:9" in (response.error or "")


async def test_lmstudio_uses_openai_compatible_schema_and_images() -> None:
    catalog = catalog_with(
        make_entry("lm", "lmstudio", "local-v", supports_vision=True, supports_json_schema=True)
    )
    transport, seen = always(openai_ok(text='{"summary": "x"}'))
    provider = LMStudioProvider(transport=transport, retry_policy=RETRY, catalog=catalog)
    await provider.safe_complete(
        ModelRequest(model_id="local-v", user_prompt="x", images=[IMAGE], response_schema=SCHEMA)
    )
    payload = body(seen[0])
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["messages"][-1]["content"][0]["type"] == "image_url"
