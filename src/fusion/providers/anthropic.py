"""Anthropic Claude API provider."""

from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx

from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse, ProviderError
from fusion.providers.http_utils import post_json_with_retries, try_parse_json

_OPUS_MINOR_NO_SAMPLING = re.compile(r"^claude-opus-4-(\d+)(?:-|$)")


def supports_sampling_params(model_id: str) -> bool:
    """Return False for Opus 4.7+ models that reject temperature/top_p/top_k."""
    match = _OPUS_MINOR_NO_SAMPLING.match(model_id)
    if match is None:
        return True
    return int(match.group(1)) < 7


class AnthropicProvider(ModelProvider):
    """Calls the Anthropic Messages API directly."""

    name = "anthropic"
    _API_URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key: str | None = None, timeout: float = 120.0) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self._timeout = timeout

    def is_available(self) -> bool:
        return bool(self._api_key)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._api_key:
            raise ProviderError("ANTHROPIC_API_KEY not configured")

        messages = [
            {"role": m.role, "content": m.content}
            for m in request.resolved_messages()
            if m.role in {"user", "assistant"}
        ]
        if not messages:
            raise ProviderError("Anthropic requires at least one user message")

        payload: dict[str, Any] = {
            "model": request.model_id,
            "max_tokens": request.max_tokens,
            "messages": messages,
        }
        if request.temperature is not None and supports_sampling_params(request.model_id):
            payload["temperature"] = request.temperature
        if request.system_prompt:
            payload["system"] = request.system_prompt
        elif request.resolved_messages() and request.resolved_messages()[0].role == "system":
            payload["system"] = request.resolved_messages()[0].content

        start = time.perf_counter()
        timeout = request.effective_timeout(self._timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            data = await post_json_with_retries(
                client=client,
                url=self._API_URL,
                provider=self.name,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json_payload=payload,
            )
        latency_ms = (time.perf_counter() - start) * 1000

        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        usage = data.get("usage", {})
        parsed = try_parse_json(text) if request.json_mode else None
        return ModelResponse(
            provider=self.name,
            model=request.model_id,
            text=text,
            parsed_json=parsed,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            latency_ms=latency_ms,
            finish_reason=data.get("stop_reason"),
            raw_response=data,
        )
