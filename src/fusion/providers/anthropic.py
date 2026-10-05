"""Anthropic Claude API provider."""

from __future__ import annotations

import base64
import os
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from fusion.config.catalog import Catalog, ModelEntry
from fusion.providers.base import (
    AuthError,
    BadRequestError,
    ImagePart,
    Message,
    ModelRequest,
    ModelResponse,
    ProviderError,
    close_objects,
)
from fusion.providers.http_utils import HttpProvider, Parsed, RetryPolicy, StreamStats
from fusion.providers.limits import ProviderLimiter

_CLAUDE_VERSION = re.compile(
    r"^claude-(?:opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?:-|$)"
)
_CACHE = {"type": "ephemeral"}


def supports_sampling_params(model_id: str) -> bool:
    """Fallback for models missing from the catalog: Claude 4.7+ rejects temperature/top_p/top_k.

    Catalog entries carry this as ``supports_sampling_params`` and take precedence.
    """
    match = _CLAUDE_VERSION.match(model_id)
    if match is None:
        return True
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return (major, minor) < (4, 7)


def _image_block(image: ImagePart) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image.media_type,
            "data": base64.b64encode(image.data).decode("ascii"),
        },
    }


def _content(
    message: Message, *, cache: bool, images: list[ImagePart]
) -> str | list[dict[str, Any]]:
    """Plain string, or content blocks when the message carries images or a cache breakpoint."""
    if not cache and not images:
        return message.content
    text_block: dict[str, Any] = {"type": "text", "text": message.content}
    if cache:
        text_block["cache_control"] = _CACHE
    return [*(_image_block(i) for i in images), text_block]


class AnthropicProvider(HttpProvider):
    """Calls the Anthropic Messages API directly."""

    name = "anthropic"
    _API_URL = "https://api.anthropic.com/v1/messages"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 120.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_policy: RetryPolicy | None = None,
        limiter: ProviderLimiter | None = None,
        catalog: Catalog | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        super().__init__(
            credential=key,
            timeout=timeout,
            http2=True,
            transport=transport,
            retry_policy=retry_policy,
            limiter=limiter,
            catalog=catalog,
            clock=clock,
        )

    def is_available(self) -> bool:
        return bool(self._api_key)

    # -- request mapping -------------------------------------------------------------------

    def _supports_sampling(self, request: ModelRequest, entry: ModelEntry | None) -> bool:
        if entry is not None:
            return entry.supports_sampling_params
        return supports_sampling_params(request.model_id)

    def build_payload(self, request: ModelRequest) -> dict[str, Any]:
        entry = self.entry_for(request.model_id)
        self.check_images(request, entry)
        turns = request.chat_messages()
        if not turns:
            raise BadRequestError("Anthropic requires at least one user message")

        mode = self.structured_mode(request, entry)
        system = self.system_with_schema(request, mode)
        last = len(turns) - 1
        messages: list[dict[str, Any]] = []
        for index, turn in enumerate(turns):
            images = request.images if index == last and turn.role == "user" else []
            # Cache everything before the final message: the shared, reusable prefix.
            cache = request.cache_prefix and index == last - 1
            messages.append(
                {"role": turn.role, "content": _content(turn, cache=cache, images=images)}
            )

        payload: dict[str, Any] = {
            "model": request.model_id,
            "max_tokens": request.max_tokens,
            "messages": messages,
        }
        if system:
            payload["system"] = (
                [{"type": "text", "text": system, "cache_control": _CACHE}]
                if request.cache_prefix
                else system
            )
        if request.thinking_budget_tokens is not None:
            if entry is not None and entry.supports_reasoning_effort:
                msg = (
                    f"{request.model_id} uses adaptive thinking; thinking_budget_tokens is "
                    "rejected by the API. Use reasoning_effort instead."
                )
                raise BadRequestError(msg)
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": request.thinking_budget_tokens,
            }
        elif request.temperature is not None and self._supports_sampling(request, entry):
            payload["temperature"] = request.temperature

        output_config: dict[str, Any] = {}
        effort = self.effective_effort(request, entry)
        if effort is not None:
            output_config["effort"] = effort
        if mode == "native" and request.response_schema is not None:
            # Forced tool_choice is rejected by Claude 5.x; native structured output is not.
            output_config["format"] = {
                "type": "json_schema",
                "schema": close_objects(request.response_schema),
            }
        if output_config:
            payload["output_config"] = output_config
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    # -- response mapping ------------------------------------------------------------------

    @staticmethod
    def _apply_usage(parsed: Parsed, usage: dict[str, Any], *, final_output: bool = True) -> None:
        """Anthropic's ``input_tokens`` excludes cache reads/writes; add them back."""
        if "input_tokens" in usage:
            reads = usage.get("cache_read_input_tokens") or 0
            writes = usage.get("cache_creation_input_tokens") or 0
            parsed.input_tokens = (usage.get("input_tokens") or 0) + reads + writes
            parsed.cached_input_tokens = usage.get("cache_read_input_tokens")
            parsed.cache_write_tokens = usage.get("cache_creation_input_tokens")
        if final_output and usage.get("output_tokens") is not None:
            parsed.output_tokens = usage["output_tokens"]

    def _parse(self, data: dict[str, Any]) -> Parsed:
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        parsed = Parsed(text=text, finish_reason=data.get("stop_reason"), raw=data)
        self._apply_usage(parsed, data.get("usage", {}))
        return parsed

    # -- completion ------------------------------------------------------------------------

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._api_key:
            raise AuthError("ANTHROPIC_API_KEY not configured")
        payload = self.build_payload(request)
        timeout = request.effective_timeout(self._timeout)
        start = self._clock()
        if request.stream:
            return await self._complete_stream(request, payload, timeout, start)
        result = await self.post_json(
            self._API_URL, headers=self._headers(), payload=payload, timeout=timeout
        )
        latency_ms = (self._clock() - start) * 1000
        return self.build_response(
            request,
            self._parse(result.data),
            latency_ms=latency_ms,
            ttft_ms=None,
            retries=result.retries,
        )

    async def _complete_stream(
        self, request: ModelRequest, payload: dict[str, Any], timeout: float, start: float
    ) -> ModelResponse:
        stats = StreamStats()
        parsed = Parsed(raw={"streamed": True})
        pieces: list[str] = []
        ttft_ms: float | None = None
        events = self.stream_sse(
            self._API_URL,
            headers=self._headers(),
            payload={**payload, "stream": True},
            timeout=timeout,
            stats=stats,
        )
        async for event in events:
            data = event.json()
            kind = data.get("type")
            if kind == "message_start":
                self._apply_usage(
                    parsed, data.get("message", {}).get("usage", {}), final_output=False
                )
            elif kind == "content_block_delta":
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta" and delta.get("text"):
                    if ttft_ms is None:
                        ttft_ms = (self._clock() - start) * 1000
                    pieces.append(delta["text"])
            elif kind == "message_delta":
                parsed.finish_reason = data.get("delta", {}).get(
                    "stop_reason", parsed.finish_reason
                )
                usage = data.get("usage", {})
                if usage.get("output_tokens") is not None:
                    parsed.output_tokens = usage["output_tokens"]
            elif kind == "error":
                message = self._scrub(str(data.get("error", {}).get("message", "stream error")))
                raise ProviderError(f"anthropic stream error: {message}", retries=stats.retries)
        parsed.text = "".join(pieces)
        latency_ms = (self._clock() - start) * 1000
        return self.build_response(
            request, parsed, latency_ms=latency_ms, ttft_ms=ttft_ms, retries=stats.retries
        )
