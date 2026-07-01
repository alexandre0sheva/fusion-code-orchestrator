"""LM Studio OpenAI-compatible local provider."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from fusion.config.env import is_local_provider_enabled
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse, ProviderError
from fusion.providers.http_utils import post_json_with_retries, try_parse_json


class LMStudioProvider(ModelProvider):
    """Calls LM Studio via its OpenAI-compatible API."""

    name = "lmstudio"

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        default = "http://localhost:1234/v1"
        self._base_url = (base_url or os.environ.get("LMSTUDIO_BASE_URL", default)).rstrip("/")
        self._timeout = timeout

    def is_available(self) -> bool:
        return is_local_provider_enabled(self.name)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        url = f"{self._base_url}/chat/completions"
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
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                data = await post_json_with_retries(
                    client=client,
                    url=url,
                    provider=self.name,
                    json_payload=payload,
                )
        except httpx.ConnectError as exc:
            raise ProviderError(f"LM Studio not reachable at {self._base_url}") from exc
        latency_ms = (time.perf_counter() - start) * 1000

        choice = data["choices"][0]
        text = choice["message"]["content"] or ""
        usage = data.get("usage", {})
        parsed = try_parse_json(text) if request.json_mode else None
        return ModelResponse(
            provider=self.name,
            model=request.model_id,
            text=text,
            parsed_json=parsed,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason"),
            raw_response=data,
        )
