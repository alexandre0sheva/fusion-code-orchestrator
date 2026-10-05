"""OpenAI Chat Completions provider (and the shared base for OpenAI-compatible servers)."""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from fusion.config.catalog import Catalog, ModelEntry
from fusion.providers.base import (
    AuthError,
    ImagePart,
    Message,
    ModelRequest,
    ModelResponse,
    ProviderError,
)
from fusion.providers.http_utils import HttpProvider, Parsed, RetryPolicy, StreamStats
from fusion.providers.limits import ProviderLimiter


def _image_part(image: ImagePart) -> dict[str, Any]:
    encoded = base64.b64encode(image.data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{image.media_type};base64,{encoded}"}}


class OpenAICompatibleProvider(HttpProvider):
    """Chat Completions request/response mapping shared by OpenAI and LM Studio."""

    _unreachable: str | None = None

    def _chat_url(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _token_limit_key(self, entry: ModelEntry | None) -> str:
        return "max_tokens"

    # -- request mapping -------------------------------------------------------------------

    def build_payload(self, request: ModelRequest) -> dict[str, Any]:
        entry = self.entry_for(request.model_id)
        self.check_images(request, entry)
        mode = self.structured_mode(request, entry)
        system = self.system_with_schema(request, mode)

        turns = request.chat_messages()
        last = len(turns) - 1
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        for index, turn in enumerate(turns):
            messages.append(self._message(turn, request.images if index == last else []))

        payload: dict[str, Any] = {
            "model": request.model_id,
            "messages": messages,
            self._token_limit_key(entry): request.max_tokens,
        }
        if request.temperature is not None and (entry is None or entry.supports_sampling_params):
            payload["temperature"] = request.temperature
        effort = self.effective_effort(request, entry)
        if effort is not None:
            payload["reasoning_effort"] = effort

        if mode == "native" and request.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.response_schema_name,
                    "strict": request.response_schema_strict,
                    "schema": request.response_schema,
                },
            }
        elif request.json_mode or (mode == "prompt" and entry is not None and entry.supports_json):
            payload["response_format"] = {"type": "json_object"}
        return payload

    @staticmethod
    def _message(turn: Message, images: list[ImagePart]) -> dict[str, Any]:
        if not images or turn.role != "user":
            return {"role": turn.role, "content": turn.content}
        parts = [*(_image_part(i) for i in images), {"type": "text", "text": turn.content}]
        return {"role": turn.role, "content": parts}

    # -- response mapping ------------------------------------------------------------------

    @staticmethod
    def _apply_usage(parsed: Parsed, usage: dict[str, Any]) -> None:
        """``prompt_tokens`` includes cached; ``completion_tokens`` includes reasoning."""
        parsed.input_tokens = usage.get("prompt_tokens", parsed.input_tokens)
        parsed.output_tokens = usage.get("completion_tokens", parsed.output_tokens)
        parsed.cached_input_tokens = (usage.get("prompt_tokens_details") or {}).get(
            "cached_tokens", parsed.cached_input_tokens
        )
        parsed.reasoning_tokens = (usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens", parsed.reasoning_tokens
        )

    def _parse(self, data: dict[str, Any]) -> Parsed:
        choice = data["choices"][0]
        parsed = Parsed(
            text=choice["message"].get("content") or "",
            finish_reason=choice.get("finish_reason"),
            raw=data,
        )
        self._apply_usage(parsed, data.get("usage") or {})
        return parsed

    # -- completion ------------------------------------------------------------------------

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload = self.build_payload(request)
        timeout = request.effective_timeout(self._timeout)
        start = self._clock()
        if request.stream:
            return await self._complete_stream(request, payload, timeout, start)
        result = await self.post_json(
            self._chat_url(),
            headers=self._headers(),
            payload=payload,
            timeout=timeout,
            unreachable=self._unreachable,
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
            self._chat_url(),
            headers=self._headers(),
            payload={**payload, "stream": True, "stream_options": {"include_usage": True}},
            timeout=timeout,
            stats=stats,
        )
        async for event in events:
            if event.data.strip() == "[DONE]":
                break
            data = event.json()
            if "error" in data:
                message = self._scrub(str(data["error"]))
                raise ProviderError(f"{self.name} stream error: {message}", retries=stats.retries)
            for choice in data.get("choices") or []:
                text = (choice.get("delta") or {}).get("content")
                if text:
                    if ttft_ms is None:
                        ttft_ms = (self._clock() - start) * 1000
                    pieces.append(text)
                if choice.get("finish_reason"):
                    parsed.finish_reason = choice["finish_reason"]
            if data.get("usage"):
                self._apply_usage(parsed, data["usage"])
        parsed.text = "".join(pieces)
        latency_ms = (self._clock() - start) * 1000
        return self.build_response(
            request, parsed, latency_ms=latency_ms, ttft_ms=ttft_ms, retries=stats.retries
        )


class OpenAIProvider(OpenAICompatibleProvider):
    """Calls the OpenAI Chat Completions API directly."""

    name = "openai"
    _API_URL = "https://api.openai.com/v1/chat/completions"

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
        key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
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

    def _chat_url(self) -> str:
        return self._API_URL

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def _token_limit_key(self, entry: ModelEntry | None) -> str:
        # Reasoning models count thinking tokens against max_completion_tokens.
        if entry is None or entry.supports_reasoning_effort:
            return "max_completion_tokens"
        return "max_tokens"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._api_key:
            raise AuthError("OPENAI_API_KEY not configured")
        return await super().complete(request)
