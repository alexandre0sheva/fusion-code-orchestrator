"""Shared HTTP machinery for provider adapters: pooled client, retries, SSE, error taxonomy."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from functools import cache
from typing import Any

import httpx

from fusion.config.catalog import Catalog, ModelEntry, load_catalog
from fusion.providers.base import (
    AuthError,
    BadRequestError,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    ServerError,
    speed_metrics,
    with_schema_instruction,
)
from fusion.providers.limits import ProviderLimiter

_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})
_REDACTED = "[redacted]"
# Key-shaped strings that must never reach logs or error text, even if a provider echoes them.
_SECRET_PATTERNS = (
    (re.compile(r"([?&](?:api[_-]?)?key)=[^&\s]+", re.IGNORECASE), rf"\1={_REDACTED}"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), _REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), _REDACTED),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE), rf"\1{_REDACTED}"),
)


class ProviderConnectionError(ProviderError):
    """The provider could not be reached (DNS, refused connection, TLS)."""

    error_type = "Connection"


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove API keys (known values and key-shaped strings) from text."""
    for secret in secrets:
        if len(secret) >= 4:
            text = text.replace(secret, _REDACTED)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


@dataclass
class RetryPolicy:
    """Retry transient failures (429/5xx/timeouts/connection) with full-jitter backoff."""

    max_attempts: int = 3
    base_delay_s: float = 0.25
    max_delay_s: float = 8.0
    # A Retry-After longer than this is surfaced as an error instead of waited for.
    max_retry_after_s: float = 60.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    jitter: Callable[[], float] = random.random

    def backoff(self, attempt: int) -> float:
        """Full jitter: uniform in [0, min(cap, base * 2**attempt)]."""
        return self.jitter() * min(self.max_delay_s, self.base_delay_s * 2.0**attempt)


@dataclass
class HttpResult:
    data: dict[str, Any]
    retries: int = 0


@dataclass
class StreamStats:
    retries: int = 0


@dataclass
class SseEvent:
    event: str | None
    data: str

    def json(self) -> dict[str, Any]:
        parsed = json.loads(self.data)
        if not isinstance(parsed, dict):
            msg = "SSE event payload is not a JSON object"
            raise ValueError(msg)
        return parsed


@dataclass
class Parsed:
    """Provider-neutral view of a completion, with token counts already normalized.

    ``input_tokens`` includes cached tokens; ``output_tokens`` includes reasoning tokens.
    """

    text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def extract_error_message(status_code: int, body: str, provider: str) -> str:
    """Build a readable error from an HTTP response body."""
    detail = body[:500] if body else f"HTTP {status_code}"
    try:
        payload = json.loads(body)
        if isinstance(payload, dict):
            for key in ("error", "message", "detail"):
                value = payload.get(key)
                if isinstance(value, str):
                    detail = value
                    break
                if isinstance(value, dict) and "message" in value:
                    detail = str(value["message"])
                    break
    except json.JSONDecodeError:
        pass
    return f"{provider} API error {status_code}: {detail}"


def classify_status(status: int, message: str) -> ProviderError:
    """Map an HTTP error status to the provider error taxonomy."""
    if status in (401, 403):
        return AuthError(message)
    if status == 408:
        return ProviderTimeoutError(message)
    if status == 429:
        return RateLimitError(message)
    if status >= 500:
        return ServerError(message)
    return BadRequestError(message)


def parse_retry_after(headers: httpx.Headers) -> float | None:
    """Seconds to wait from ``retry-after-ms`` / ``Retry-After`` (seconds or HTTP date)."""
    raw_ms = headers.get("retry-after-ms")
    if raw_ms is not None:
        try:
            return max(0.0, float(raw_ms) / 1000.0)
        except ValueError:
            pass
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def try_parse_json(text: str) -> dict[str, Any] | None:
    """Parse JSON from model text, including fenced blocks."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            parsed = json.loads(stripped[start:end])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


async def parse_sse(response: httpx.Response) -> AsyncIterator[SseEvent]:
    """Yield server-sent events from a streaming response."""
    event: str | None = None
    data: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield SseEvent(event, "\n".join(data))
            event, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield SseEvent(event, "\n".join(data))


@cache
def default_catalog() -> Catalog:
    """The packaged catalog, loaded once per process."""
    return load_catalog()


class HttpProvider(ModelProvider):
    """Base for providers that talk HTTP: one pooled client, retries, limiter, capabilities."""

    def __init__(
        self,
        *,
        credential: str = "",
        timeout: float,
        http2: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_policy: RetryPolicy | None = None,
        limiter: ProviderLimiter | None = None,
        catalog: Catalog | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._api_key = credential
        self._timeout = timeout
        # HTTP/2 multiplexes parallel panel calls over one connection; needs the optional h2 dep.
        self._http2 = http2 and importlib.util.find_spec("h2") is not None
        self._transport = transport
        self._retry = retry_policy or RetryPolicy()
        self.limiter = limiter
        self._catalog = catalog
        self._clock = clock
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle -------------------------------------------------------------------------

    @property
    def http_client(self) -> httpx.AsyncClient:
        """The shared client; created lazily and re-created if the event loop changed."""
        loop = asyncio.get_running_loop()
        if self._client is None or self._client.is_closed or self._client_loop is not loop:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                http2=self._http2,
                transport=self._transport,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            )
            self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            await client.aclose()

    # -- capabilities ----------------------------------------------------------------------

    def entry_for(self, model_id: str) -> ModelEntry | None:
        """Catalog entry for this provider's model ID, or None for unknown models."""
        return (self._catalog or default_catalog()).find(self.name, model_id)

    def effective_effort(self, request: ModelRequest, entry: ModelEntry | None) -> str | None:
        """Requested reasoning effort, else the catalog default; None if unsupported."""
        if entry is None or not entry.supports_reasoning_effort:
            return None
        return request.reasoning_effort or entry.default_reasoning_effort

    def structured_mode(self, request: ModelRequest, entry: ModelEntry | None) -> str:
        """``native`` (provider enforces the schema), ``prompt`` (spelled out) or ``none``."""
        if request.response_schema is None:
            return "none"
        return "native" if entry is not None and entry.supports_json_schema else "prompt"

    def system_with_schema(self, request: ModelRequest, mode: str) -> str:
        """System text, with the schema appended when the provider cannot enforce it."""
        system = request.system_text()
        if mode == "prompt" and request.response_schema is not None:
            return with_schema_instruction(system, request.response_schema)
        return system

    def check_images(self, request: ModelRequest, entry: ModelEntry | None) -> None:
        if request.images and entry is not None and not entry.supports_vision:
            msg = (
                f"{request.model_id} does not support image input (catalog: supports_vision=false)"
            )
            raise BadRequestError(msg)

    # -- requests --------------------------------------------------------------------------

    def _slot(self) -> AbstractAsyncContextManager[None]:
        return self.limiter.slot() if self.limiter is not None else nullcontext()

    def _scrub(self, text: str) -> str:
        return scrub(text, (self._api_key,))

    def _transport_error(self, exc: httpx.HTTPError, unreachable: str | None) -> ProviderError:
        if isinstance(exc, httpx.TimeoutException):
            return ProviderTimeoutError(f"{self.name} request timed out")
        if isinstance(exc, httpx.ConnectError) and unreachable:
            return ProviderConnectionError(unreachable)
        return ProviderConnectionError(self._scrub(f"{self.name} HTTP error: {exc}"))

    def _retry_delay(
        self, error: ProviderError, attempt: int, retry_after: float | None, *, transient: bool
    ) -> float | None:
        """Seconds to wait before the next attempt, or None when the error is final."""
        if not transient or attempt + 1 >= self._retry.max_attempts:
            return None
        if retry_after is not None:
            return retry_after if retry_after <= self._retry.max_retry_after_s else None
        return self._retry.backoff(attempt)

    def _status_error(self, response: httpx.Response, text: str) -> ProviderError:
        message = self._scrub(extract_error_message(response.status_code, text, self.name))
        return classify_status(response.status_code, message)

    async def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
        unreachable: str | None = None,
    ) -> HttpResult:
        """POST JSON; retry transient failures; raise a classified ``ProviderError``."""
        for attempt in range(self._retry.max_attempts):  # attempt == retries made so far
            retry_after: float | None = None
            try:
                async with self._slot():
                    response = await self.http_client.post(
                        url, headers=headers, json=payload, timeout=timeout
                    )
                if response.status_code == 200:
                    return HttpResult(self._decode(response), attempt)
                error = self._status_error(response, response.text)
                transient = response.status_code in _TRANSIENT_STATUS
                retry_after = parse_retry_after(response.headers)
            except httpx.HTTPError as exc:
                error = self._transport_error(exc, unreachable)
                transient = True
            delay = self._retry_delay(error, attempt, retry_after, transient=transient)
            if delay is None:
                error.retries = attempt
                raise error
            await self._retry.sleep(delay)
        raise ProviderError(f"{self.name}: no attempts were made")  # pragma: no cover

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"{self.name} returned malformed JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError(f"{self.name} returned unexpected JSON payload")
        return data

    async def stream_sse(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
        stats: StreamStats,
    ) -> AsyncIterator[SseEvent]:
        """POST and yield SSE events. Only failures before the first byte are retried."""
        for attempt in range(self._retry.max_attempts):
            retry_after: float | None = None
            started = False
            try:
                async with (
                    self._slot(),
                    self.http_client.stream(
                        "POST", url, headers=headers, json=payload, timeout=timeout
                    ) as response,
                ):
                    if response.status_code == 200:
                        async for event in parse_sse(response):
                            started = True
                            yield event
                        return
                    text = (await response.aread()).decode("utf-8", "replace")
                    error = self._status_error(response, text)
                    transient = response.status_code in _TRANSIENT_STATUS
                    retry_after = parse_retry_after(response.headers)
            except httpx.HTTPError as exc:
                error = self._transport_error(exc, None)
                transient = not started
            delay = self._retry_delay(error, attempt, retry_after, transient=transient)
            if delay is None:
                error.retries = stats.retries
                raise error
            await self._retry.sleep(delay)
            stats.retries += 1

    # -- responses -------------------------------------------------------------------------

    def build_response(
        self,
        request: ModelRequest,
        parsed: Parsed,
        *,
        latency_ms: float,
        ttft_ms: float | None,
        retries: int,
    ) -> ModelResponse:
        decode_tps, total_tps = speed_metrics(parsed.output_tokens, latency_ms, ttft_ms)
        wants_json = request.json_mode or request.response_schema is not None
        return ModelResponse(
            provider=self.name,
            model=request.model_id,
            text=parsed.text,
            parsed_json=try_parse_json(parsed.text) if wants_json else None,
            input_tokens=parsed.input_tokens,
            output_tokens=parsed.output_tokens,
            cached_input_tokens=parsed.cached_input_tokens,
            cache_write_tokens=parsed.cache_write_tokens,
            reasoning_tokens=parsed.reasoning_tokens,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            decode_tokens_per_s=decode_tps,
            total_tokens_per_s=total_tps,
            retries=retries,
            finish_reason=parsed.finish_reason,
            raw_response=parsed.raw,
        )
