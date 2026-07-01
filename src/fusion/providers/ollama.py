"""Ollama local model provider."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from fusion.config.env import is_local_provider_enabled
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse, ProviderError
from fusion.providers.http_utils import post_json_with_retries, try_parse_json


class OllamaProvider(ModelProvider):
    """Calls a local Ollama server."""

    name = "ollama"

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        default = "http://localhost:11434"
        self._base_url = (base_url or os.environ.get("OLLAMA_BASE_URL", default)).rstrip("/")
        self._timeout = timeout

    def is_available(self) -> bool:
        return is_local_provider_enabled(self.name)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        url = f"{self._base_url}/api/chat"
        messages = [
            {"role": m.role, "content": m.content}
            for m in request.resolved_messages()
            if m.role in {"system", "user", "assistant"}
        ]

        options: dict[str, Any] = {"num_predict": request.max_tokens}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        payload: dict[str, Any] = {
            "model": request.model_id,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if request.json_mode:
            payload["format"] = "json"

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
            raise ProviderError(f"Ollama not reachable at {self._base_url}") from exc
        latency_ms = (time.perf_counter() - start) * 1000

        text = data.get("message", {}).get("content", "")
        parsed = try_parse_json(text) if request.json_mode else None
        return ModelResponse(
            provider=self.name,
            model=request.model_id,
            text=text,
            parsed_json=parsed,
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            latency_ms=latency_ms,
            finish_reason=data.get("done_reason"),
            raw_response=data,
        )
