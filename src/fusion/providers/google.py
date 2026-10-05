"""Google Gemini API provider."""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from fusion.config.catalog import Catalog
from fusion.providers.base import (
    AuthError,
    ImagePart,
    ModelRequest,
    ModelResponse,
    ProviderError,
    inline_schema_refs,
)
from fusion.providers.http_utils import HttpProvider, Parsed, RetryPolicy, StreamStats
from fusion.providers.limits import ProviderLimiter

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
# Gemini 3.x thinking levels. `minimal` errors on 3.8 Flash, so the floor is `low`.
_THINKING_LEVELS = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}
# OpenAPI-subset schema: keys Gemini rejects.
_UNSUPPORTED_SCHEMA_KEYS = frozenset({"additionalProperties", "$schema", "$defs", "definitions"})


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$ref`` pointers and drop keywords Gemini's responseSchema does not accept."""

    def clean(node: Any) -> Any:
        if isinstance(node, list):
            return [clean(item) for item in node]
        if not isinstance(node, dict):
            return node
        return {k: clean(v) for k, v in node.items() if k not in _UNSUPPORTED_SCHEMA_KEYS}

    result: dict[str, Any] = clean(inline_schema_refs(schema))
    return result


def _image_part(image: ImagePart) -> dict[str, Any]:
    return {
        "inlineData": {
            "mimeType": image.media_type,
            "data": base64.b64encode(image.data).decode("ascii"),
        }
    }


class GoogleProvider(HttpProvider):
    """Calls the Google Generative Language API directly."""

    name = "google"

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
        key = api_key if api_key is not None else os.environ.get("GOOGLE_API_KEY", "")
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

    def _url(self, model_id: str, *, stream: bool) -> str:
        if stream:
            return f"{_BASE_URL}/{model_id}:streamGenerateContent?alt=sse"
        return f"{_BASE_URL}/{model_id}:generateContent"

    def _headers(self) -> dict[str, str]:
        # The key goes in a header: URLs end up in logs and exception strings.
        return {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

    # -- request mapping -------------------------------------------------------------------

    def build_payload(self, request: ModelRequest) -> dict[str, Any]:
        entry = self.entry_for(request.model_id)
        self.check_images(request, entry)
        mode = self.structured_mode(request, entry)
        system = self.system_with_schema(request, mode)

        turns = request.chat_messages()
        last = len(turns) - 1
        contents: list[dict[str, Any]] = []
        for index, turn in enumerate(turns):
            parts: list[dict[str, Any]] = []
            if index == last and turn.role == "user":
                parts.extend(_image_part(i) for i in request.images)
            parts.append({"text": turn.content})
            contents.append(
                {"role": "model" if turn.role == "assistant" else "user", "parts": parts}
            )

        config: dict[str, Any] = {"maxOutputTokens": request.max_tokens}
        if request.temperature is not None and (entry is None or entry.supports_sampling_params):
            config["temperature"] = request.temperature
        if request.seed is not None and (entry is None or entry.supports_sampling_params):
            config["seed"] = request.seed
        effort = self.effective_effort(request, entry)
        if effort is not None:
            config["thinkingConfig"] = {"thinkingLevel": _THINKING_LEVELS.get(effort, "medium")}
        if mode == "native" and request.response_schema is not None:
            config["responseMimeType"] = "application/json"
            config["responseSchema"] = to_gemini_schema(request.response_schema)
        elif request.json_mode or mode == "prompt":
            config["responseMimeType"] = "application/json"

        payload: dict[str, Any] = {"contents": contents, "generationConfig": config}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return payload

    # -- response mapping ------------------------------------------------------------------

    @staticmethod
    def _apply_usage(parsed: Parsed, usage: dict[str, Any]) -> None:
        """Thinking tokens are billed as output but reported outside ``candidatesTokenCount``."""
        if "promptTokenCount" in usage:
            parsed.input_tokens = usage["promptTokenCount"]
        if "candidatesTokenCount" in usage or "thoughtsTokenCount" in usage:
            thoughts = usage.get("thoughtsTokenCount") or 0
            parsed.output_tokens = (usage.get("candidatesTokenCount") or 0) + thoughts
            parsed.reasoning_tokens = thoughts or None
        if "cachedContentTokenCount" in usage:
            parsed.cached_input_tokens = usage["cachedContentTokenCount"]

    @staticmethod
    def _candidate_text(data: dict[str, Any]) -> tuple[str, str | None]:
        text, finish = "", None
        for candidate in data.get("candidates", []):
            finish = candidate.get("finishReason", finish)
            for part in candidate.get("content", {}).get("parts", []):
                if not part.get("thought"):
                    text += part.get("text", "")
        return text, finish

    def _parse(self, data: dict[str, Any]) -> Parsed:
        text, finish = self._candidate_text(data)
        parsed = Parsed(text=text, finish_reason=finish, raw=data)
        self._apply_usage(parsed, data.get("usageMetadata", {}))
        return parsed

    # -- completion ------------------------------------------------------------------------

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._api_key:
            raise AuthError("GOOGLE_API_KEY not configured")
        payload = self.build_payload(request)
        timeout = request.effective_timeout(self._timeout)
        start = self._clock()
        if request.stream:
            return await self._complete_stream(request, payload, timeout, start)
        result = await self.post_json(
            self._url(request.model_id, stream=False),
            headers=self._headers(),
            payload=payload,
            timeout=timeout,
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
            self._url(request.model_id, stream=True),
            headers=self._headers(),
            payload=payload,
            timeout=timeout,
            stats=stats,
        )
        async for event in events:
            data = event.json()
            if "error" in data:
                message = self._scrub(str(data["error"]))
                raise ProviderError(f"google stream error: {message}", retries=stats.retries)
            text, finish = self._candidate_text(data)
            if text:
                if ttft_ms is None:
                    ttft_ms = (self._clock() - start) * 1000
                pieces.append(text)
            parsed.finish_reason = finish or parsed.finish_reason
            if "usageMetadata" in data:  # cumulative: the last event carries the totals
                self._apply_usage(parsed, data["usageMetadata"])
        parsed.text = "".join(pieces)
        latency_ms = (self._clock() - start) * 1000
        return self.build_response(
            request, parsed, latency_ms=latency_ms, ttft_ms=ttft_ms, retries=stats.retries
        )
