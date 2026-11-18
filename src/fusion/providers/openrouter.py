"""OpenRouter: open-weight models, and a fallback route for vendor models (Chat Completions).

Every request asks OpenRouter for the billed cost (``usage: {include: true}``), which the ledger
records instead of the catalog estimate, and carries the data policy from the catalog's
``openrouter:`` block: ``data_collection: "allow"`` (providers that store prompts without training
on them are fine), ``require_parameters: true`` and an ``ignore`` list of providers that may train
on prompts. The account-level "no training" setting is the other half of that rule (see
``docs/CONFIGURATION.md``). Only a generic ``X-Title`` is sent, never anything about the user.

Anthropic models cache a prompt only at explicit breakpoints, also through OpenRouter, so a request
with ``cache_prefix`` for an ``anthropic/`` model marks the system prompt and the shared part of
the user prompt (``shared_prefix_chars``) with ``cache_control``, as the Anthropic adapter does.
Other models cache identical prefixes by themselves and are sent plain text.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from fusion.config.catalog import Catalog, ModelEntry
from fusion.providers.base import AuthError, ModelRequest, ModelResponse, ProviderError
from fusion.providers.http_utils import Parsed, RetryPolicy, default_catalog
from fusion.providers.limits import ProviderLimiter
from fusion.providers.openai import OpenAICompatibleProvider

BASE_URL = "https://openrouter.ai/api/v1"
KEY_ENV = "OPENROUTER_API_KEY"
APP_TITLE = "fusion-code-orchestrator"
_CACHE = {"type": "ephemeral"}
# OpenRouter IDs of models that cache only at explicit breakpoints.
_EXPLICIT_CACHE_PREFIXES = ("anthropic/",)


def _cached_text(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``content`` as parts with a breakpoint on its (last) text part."""
    if isinstance(content, str):
        return [{"type": "text", "text": content, "cache_control": _CACHE}]
    parts = [dict(part) for part in content]
    for part in reversed(parts):
        if part.get("type") == "text":
            part["cache_control"] = _CACHE
            break
    return parts


def _split_text(
    content: str | list[dict[str, Any]], split: int | None
) -> str | list[dict[str, Any]]:
    """The final user message with a breakpoint after its first ``split`` characters of text;
    unchanged when there is no usable split. The text the model reads is the same."""
    parts = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
    index = next((i for i in range(len(parts) - 1, -1, -1) if parts[i].get("type") == "text"), -1)
    text = str(parts[index].get("text", "")) if index >= 0 else ""
    if split is None or not 0 < split < len(text):
        return content
    head = {"type": "text", "text": text[:split], "cache_control": _CACHE}
    tail = {"type": "text", "text": text[split:]}
    return [*parts[:index], head, tail, *parts[index + 1 :]]


class OpenRouterProvider(OpenAICompatibleProvider):
    """Calls OpenRouter's OpenAI-compatible Chat Completions API."""

    name = "openrouter"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 120.0,
        *,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_policy: RetryPolicy | None = None,
        limiter: ProviderLimiter | None = None,
        catalog: Catalog | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get(KEY_ENV, "")
        self.base_url = base_url.rstrip("/")
        super().__init__(
            credential=key.strip(),
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

    def _catalog_or_default(self) -> Catalog:
        return self._catalog or default_catalog()

    def entry_for(self, model_id: str) -> ModelEntry | None:
        """The catalog entry served under this OpenRouter ID (an open-weight or vendor model)."""
        return self._catalog_or_default().find_openrouter(model_id)

    def _chat_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Title": APP_TITLE,
        }

    def build_payload(self, request: ModelRequest) -> dict[str, Any]:
        payload = super().build_payload(request)
        effort = payload.pop("reasoning_effort", None)
        if effort is not None:
            payload["reasoning"] = {"effort": effort}
        payload["usage"] = {"include": True}
        routing: dict[str, Any] = {"require_parameters": True, "data_collection": "allow"}
        ignored = self._catalog_or_default().openrouter.provider_ignore
        if ignored:
            routing["ignore"] = list(ignored)
        payload["provider"] = routing
        if request.cache_prefix and request.model_id.startswith(_EXPLICIT_CACHE_PREFIXES):
            self._mark_cache(payload["messages"], request.shared_prefix_chars)
        return payload

    @staticmethod
    def _mark_cache(messages: list[dict[str, Any]], split: int | None) -> None:
        """Breakpoints on the system prompt, the message before the last (a conversation's
        reusable history) and the shared part of a final user message."""
        if messages and messages[0]["role"] == "system":
            messages[0]["content"] = _cached_text(messages[0]["content"])
        turns = [m for m in messages if m["role"] != "system"]
        if len(turns) >= 2:
            turns[-2]["content"] = _cached_text(turns[-2]["content"])
        if turns and turns[-1]["role"] == "user":
            turns[-1]["content"] = _split_text(turns[-1]["content"], split)

    @staticmethod
    def _apply_usage(parsed: Parsed, usage: dict[str, Any]) -> None:
        OpenAICompatibleProvider._apply_usage(parsed, usage)
        cost = usage.get("cost")
        if isinstance(cost, int | float) and not isinstance(cost, bool):
            parsed.actual_cost_usd = float(cost)

    def _parse(self, data: dict[str, Any]) -> Parsed:
        if not data.get("choices") and "error" in data:
            error = data["error"]
            detail = error.get("message", error) if isinstance(error, dict) else error
            raise ProviderError(self._scrub(f"openrouter error: {detail}"))
        return super()._parse(data)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self._api_key:
            raise AuthError(f"{KEY_ENV} not configured")
        return await super().complete(request)
