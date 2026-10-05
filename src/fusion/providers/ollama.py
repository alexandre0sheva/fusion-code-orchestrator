"""Ollama local model provider."""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from fusion.config.catalog import Catalog
from fusion.config.env import is_local_provider_enabled
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.providers.http_utils import HttpProvider, Parsed, RetryPolicy
from fusion.providers.limits import ProviderLimiter


class OllamaProvider(HttpProvider):
    """Calls a local Ollama server (non-streaming; speed is derived from total latency)."""

    name = "ollama"

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 300.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_policy: RetryPolicy | None = None,
        limiter: ProviderLimiter | None = None,
        catalog: Catalog | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        default = "http://localhost:11434"
        self._base_url = (base_url or os.environ.get("OLLAMA_BASE_URL", default)).rstrip("/")
        super().__init__(
            timeout=timeout,
            transport=transport,
            retry_policy=retry_policy,
            limiter=limiter,
            catalog=catalog,
            clock=clock,
        )

    def is_available(self) -> bool:
        return is_local_provider_enabled(self.name)

    def build_payload(self, request: ModelRequest) -> dict[str, Any]:
        entry = self.entry_for(request.model_id)
        self.check_images(request, entry)
        mode = self.structured_mode(request, entry)
        system = self.system_with_schema(request, mode)

        turns = request.chat_messages()
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend({"role": t.role, "content": t.content} for t in turns)
        if request.images:
            messages[-1]["images"] = [
                base64.b64encode(i.data).decode("ascii") for i in request.images
            ]

        options: dict[str, Any] = {"num_predict": request.max_tokens}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.seed is not None:
            options["seed"] = request.seed
        payload: dict[str, Any] = {
            "model": request.model_id,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if mode == "native" and request.response_schema is not None:
            payload["format"] = request.response_schema
        elif request.json_mode or mode == "prompt":
            payload["format"] = "json"
        return payload

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload = self.build_payload(request)
        timeout = request.effective_timeout(self._timeout)
        start = self._clock()
        result = await self.post_json(
            f"{self._base_url}/api/chat",
            headers={"Content-Type": "application/json"},
            payload=payload,
            timeout=timeout,
            unreachable=f"Ollama not reachable at {self._base_url}",
        )
        latency_ms = (self._clock() - start) * 1000
        data = result.data
        parsed = Parsed(
            text=data.get("message", {}).get("content", ""),
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            finish_reason=data.get("done_reason"),
            raw=data,
        )
        return self.build_response(
            request, parsed, latency_ms=latency_ms, ttft_ms=None, retries=result.retries
        )
