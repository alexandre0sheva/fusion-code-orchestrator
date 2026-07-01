"""OpenAI API provider."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse, ProviderError
from fusion.providers.http_utils import post_json_with_retries, try_parse_json


class OpenAIProvider(ModelProvider):
    """Calls the OpenAI Chat Completions API directly."""

    name = "openai"
    _API_URL = "https://api.openai.com/v1/chat/completions"

    def __init__(self, api_key: str | None = None, timeout: float = 120.0) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        self._timeout = timeout

    def is_available(self) -> bool:
        return bool(self._api_key)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._api_key:
            raise ProviderError("OPENAI_API_KEY not configured")

        messages = [{"role": m.role, "content": m.content} for m in request.resolved_messages()]
        payload: dict[str, Any] = {
            "model": request.model_id,
            "messages": messages,
            "max_tokens": request.max_tokens,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}

        start = time.perf_counter()
        timeout = request.effective_timeout(self._timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await post_json_with_retries(
                client=client,
                url=self._API_URL,
                provider=self.name,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json_payload=payload,
            )
        latency_ms = (time.perf_counter() - start) * 1000

        choice = data["choices"][0]
        text = choice["message"]["content"] or ""
        usage = data.get("usage", {})
        prompt_details = usage.get("prompt_tokens_details", {})
        completion_details = usage.get("completion_tokens_details", {})
        parsed = try_parse_json(text) if request.json_mode else None
        return ModelResponse(
            provider=self.name,
            model=request.model_id,
            text=text,
            parsed_json=parsed,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            cached_input_tokens=prompt_details.get("cached_tokens"),
            reasoning_tokens=completion_details.get("reasoning_tokens"),
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason"),
            raw_response=data,
        )
