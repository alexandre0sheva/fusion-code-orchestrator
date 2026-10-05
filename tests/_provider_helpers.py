"""Shared helpers for provider adapter tests (no network: httpx.MockTransport only)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import date
from typing import Any

import httpx

from fusion.config.catalog import Catalog, ModelEntry, PriceSchedule

Handler = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSleep:
    """Records requested sleeps; optionally advances a FakeClock instead of waiting."""

    def __init__(self, clock: FakeClock | None = None) -> None:
        self.calls: list[float] = []
        self._clock = clock

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._clock is not None:
            self._clock.advance(seconds)


class ScriptedStream(httpx.AsyncByteStream):
    """SSE body that advances a FakeClock before each chunk, simulating arrival times."""

    def __init__(self, clock: FakeClock, chunks: list[tuple[float, str]]) -> None:
        self._clock = clock
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for delay, text in self._chunks:
            self._clock.advance(delay)
            yield text.encode()


def sse(*events: tuple[str | None, dict[str, Any] | str]) -> str:
    """Build an SSE document from (event, data) pairs."""
    out: list[str] = []
    for name, data in events:
        if name:
            out.append(f"event: {name}")
        payload = data if isinstance(data, str) else json.dumps(data)
        out.append(f"data: {payload}")
        out.append("")
    return "\n".join(out) + "\n"


def sse_response(
    clock: FakeClock, chunks: list[tuple[float, str]], status: int = 200
) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"content-type": "text/event-stream"},
        stream=ScriptedStream(clock, chunks),
    )


def sequence(
    *responses: httpx.Response | Exception,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Transport that answers requests in order, recording each one."""
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler), seen


def always(response: httpx.Response) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    return sequence(response)


def body(request: httpx.Request) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(request.content)
    return parsed


def catalog_with(*entries: ModelEntry) -> Catalog:
    return Catalog(models={e.alias: e for e in entries})


def make_entry(alias: str, provider: str, model_id: str, **flags: Any) -> ModelEntry:
    price = PriceSchedule(
        input_per_1m=1.0,
        output_per_1m=2.0,
        verified_on=date(2026, 10, 5),
        source_url="https://example.test/pricing",
    )
    return ModelEntry(alias=alias, provider=provider, model_id=model_id, prices=[price], **flags)


# Minimal valid provider payloads ------------------------------------------------------------


def anthropic_ok(
    text: str = "hi", usage: dict[str, int] | None = None, **extra: Any
) -> httpx.Response:
    payload = {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
        **extra,
    }
    return httpx.Response(200, json=payload)


def openai_ok(text: str = "hi", usage: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def google_ok(text: str = "hi", usage: dict[str, int] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "usageMetadata": usage or {"promptTokenCount": 10, "candidatesTokenCount": 5},
        },
    )


PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "items": {"type": "array", "items": {"$ref": "#/$defs/Item"}},
    },
    "required": ["summary"],
    "$defs": {
        "Item": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
    },
}
