"""Google Gemini API provider."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse, ProviderError
from fusion.providers.http_utils import post_json_with_retries, try_parse_json


class GoogleProvider(ModelProvider):
    """Calls the Google Generative Language API directly."""

    name = "google"

    def __init__(self, api_key: str | None = None, timeout: float = 120.0) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("GOOGLE_API_KEY", "")
        self._timeout = timeout

    def _url(self, model_id: str) -> str:
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model_id}:generateContent?key={self._api_key}"
        )

    def is_available(self) -> bool:
        return bool(self._api_key)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._api_key:
            raise ProviderError("GOOGLE_API_KEY not configured")

        parts: list[dict[str, str]] = []
        for message in request.resolved_messages():
            if message.role == "system" or message.role == "user" or message.role == "assistant":
                parts.append({"text": message.content})

        generation_config: dict[str, Any] = {
            "maxOutputTokens": request.max_tokens,
        }
        if request.temperature is not None:
            generation_config["temperature"] = request.temperature
        if request.json_mode:
            generation_config["responseMimeType"] = "application/json"

        payload = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation_config,
        }

        start = time.perf_counter()
        timeout = request.effective_timeout(self._timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await post_json_with_retries(
                client=client,
                url=self._url(request.model_id),
                provider=self.name,
                json_payload=payload,
            )
        latency_ms = (time.perf_counter() - start) * 1000

        text = ""
        finish_reason: str | None = None
        for candidate in data.get("candidates", []):
            finish_reason = candidate.get("finishReason")
            for part in candidate.get("content", {}).get("parts", []):
                text += part.get("text", "")

        usage = data.get("usageMetadata", {})
        parsed = try_parse_json(text) if request.json_mode else None
        return ModelResponse(
            provider=self.name,
            model=request.model_id,
            text=text,
            parsed_json=parsed,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
            cached_input_tokens=usage.get("cachedContentTokenCount"),
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            raw_response=data,
        )
