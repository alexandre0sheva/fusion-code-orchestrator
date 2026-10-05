"""SSE streaming and speed metrics (TTFT, decode and total tokens/s) with scripted timings."""

from __future__ import annotations

import httpx
import pytest

from _provider_helpers import (
    FakeClock,
    FakeSleep,
    body,
    google_ok,
    openai_ok,
    sse,
    sse_response,
)
from fusion.providers.anthropic import AnthropicProvider
from fusion.providers.base import ModelRequest
from fusion.providers.google import GoogleProvider
from fusion.providers.http_utils import RetryPolicy
from fusion.providers.openai import OpenAIProvider

HAIKU = "claude-haiku-4-5-20251001"


def _retry() -> RetryPolicy:
    return RetryPolicy(sleep=FakeSleep(), jitter=lambda: 0.0)


def _transport(handler: httpx.MockTransport) -> httpx.MockTransport:
    return handler


async def test_anthropic_stream_collects_text_usage_and_timings() -> None:
    clock = FakeClock()
    chunks = [
        (
            0.0,
            sse(
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "usage": {
                                "input_tokens": 10,
                                "cache_read_input_tokens": 5,
                                "cache_creation_input_tokens": 0,
                                "output_tokens": 1,
                            }
                        },
                    },
                ),
                ("ping", {"type": "ping"}),
            ),
        ),
        (
            0.5,
            sse(
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}},
                )
            ),
        ),
        (
            0.5,
            sse(
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}},
                )
            ),
        ),
        (
            1.0,
            sse(
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {"output_tokens": 20},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ),
        ),
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return sse_response(clock, chunks)

    provider = AnthropicProvider(
        api_key="ak", transport=httpx.MockTransport(handler), retry_policy=_retry(), clock=clock
    )
    response = await provider.safe_complete(
        ModelRequest(model_id=HAIKU, user_prompt="hi", stream=True)
    )
    assert body(seen[0])["stream"] is True
    assert response.ok, response.error
    assert response.text == "Hello"
    assert response.finish_reason == "end_turn"
    assert response.input_tokens == 15  # 10 + 5 cache reads
    assert response.cached_input_tokens == 5
    assert response.output_tokens == 20
    assert response.ttft_ms == pytest.approx(500.0)
    assert response.latency_ms == pytest.approx(2000.0)
    assert response.total_tokens_per_s == pytest.approx(10.0)
    assert response.decode_tokens_per_s == pytest.approx(20 / 1.5)


async def test_openai_stream_requests_usage_and_measures_speed() -> None:
    clock = FakeClock()

    def chunk(content: str | None, usage: dict[str, object] | None = None) -> str:
        data: dict[str, object] = {
            "choices": [{"delta": {"content": content}, "finish_reason": None}]
        }
        if usage is not None:
            data = {"choices": [], "usage": usage}
        return sse((None, data))

    chunks = [
        (0.0, sse((None, {"choices": [{"delta": {"role": "assistant", "content": ""}}]}))),
        (0.4, chunk("foo")),
        (0.6, chunk("bar")),
        (
            1.0,
            chunk(
                None,
                {
                    "prompt_tokens": 12,
                    "completion_tokens": 40,
                    "prompt_tokens_details": {"cached_tokens": 2},
                    "completion_tokens_details": {"reasoning_tokens": 15},
                },
            )
            + sse((None, "[DONE]")),
        ),
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return sse_response(clock, chunks)

    provider = OpenAIProvider(
        api_key="sk", transport=httpx.MockTransport(handler), retry_policy=_retry(), clock=clock
    )
    response = await provider.safe_complete(
        ModelRequest(model_id="gpt-6-luna", user_prompt="hi", stream=True)
    )
    payload = body(seen[0])
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert response.text == "foobar"
    assert (response.input_tokens, response.output_tokens) == (12, 40)
    assert response.reasoning_tokens == 15
    assert response.cached_input_tokens == 2
    assert response.ttft_ms == pytest.approx(400.0)  # empty role-only delta is not a token
    assert response.latency_ms == pytest.approx(2000.0)
    assert response.decode_tokens_per_s == pytest.approx(40 / 1.6)
    assert response.total_tokens_per_s == pytest.approx(20.0)


async def test_google_stream_uses_sse_endpoint_and_final_usage() -> None:
    clock = FakeClock()

    def event(text: str, usage: dict[str, int] | None = None) -> str:
        data: dict[str, object] = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        if usage:
            data["usageMetadata"] = usage
        return sse((None, data))

    chunks = [
        (0.3, event("a")),
        (
            0.7,
            event(
                "b", {"promptTokenCount": 9, "candidatesTokenCount": 20, "thoughtsTokenCount": 10}
            ),
        ),
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return sse_response(clock, chunks)

    provider = GoogleProvider(
        api_key="gk", transport=httpx.MockTransport(handler), retry_policy=_retry(), clock=clock
    )
    response = await provider.safe_complete(
        ModelRequest(model_id="gemini-3.8-flash", user_prompt="hi", stream=True)
    )
    request = seen[0]
    assert request.url.path.endswith(":streamGenerateContent")
    assert request.url.params["alt"] == "sse"
    assert "gk" not in str(request.url)
    assert response.text == "ab"
    assert (response.input_tokens, response.output_tokens, response.reasoning_tokens) == (9, 30, 10)
    assert response.ttft_ms == pytest.approx(300.0)
    assert response.latency_ms == pytest.approx(1000.0)
    assert response.decode_tokens_per_s == pytest.approx(30 / 0.7)


async def test_stream_retries_a_transient_status_before_any_bytes_arrive() -> None:
    clock = FakeClock()
    sleeps = FakeSleep(clock)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(529, headers={"Retry-After": "1"})
        return sse_response(
            clock,
            [
                (0.5, sse((None, {"choices": [{"delta": {"content": "ok"}}]}))),
                (
                    0.5,
                    sse(
                        (
                            None,
                            {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
                        ),
                        (None, "[DONE]"),
                    ),
                ),
            ],
        )

    provider = OpenAIProvider(
        api_key="sk",
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(sleep=sleeps, jitter=lambda: 0.0),
        clock=clock,
    )
    response = await provider.safe_complete(
        ModelRequest(model_id="gpt-6-luna", user_prompt="hi", stream=True)
    )
    assert response.ok, response.error
    assert response.retries == 1
    assert sleeps.calls == [1.0]
    assert response.text == "ok"


async def test_stream_error_status_is_classified() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider = OpenAIProvider(
        api_key="sk", transport=httpx.MockTransport(handler), retry_policy=_retry(), clock=clock
    )
    response = await provider.safe_complete(
        ModelRequest(model_id="gpt-6-luna", user_prompt="hi", stream=True)
    )
    assert response.error_type == "Auth"
    assert "bad key" in (response.error or "")


async def test_non_streaming_calls_report_only_total_tokens_per_second() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        clock.advance(2.0)
        return openai_ok(usage={"prompt_tokens": 10, "completion_tokens": 40})

    provider = OpenAIProvider(
        api_key="sk", transport=httpx.MockTransport(handler), retry_policy=_retry(), clock=clock
    )
    response = await provider.safe_complete(ModelRequest(model_id="gpt-6-luna", user_prompt="hi"))
    assert response.latency_ms == pytest.approx(2000.0)
    assert response.ttft_ms is None
    assert response.decode_tokens_per_s is None
    assert response.total_tokens_per_s == pytest.approx(20.0)


async def test_speed_metrics_are_none_when_the_provider_reports_no_usage() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        clock.advance(1.0)
        data = google_ok().json()
        data.pop("usageMetadata")
        return httpx.Response(200, json=data)

    provider = GoogleProvider(
        api_key="gk", transport=httpx.MockTransport(handler), retry_policy=_retry(), clock=clock
    )
    response = await provider.safe_complete(
        ModelRequest(model_id="gemini-3.8-flash", user_prompt="hi")
    )
    assert response.output_tokens is None
    assert response.total_tokens_per_s is None
