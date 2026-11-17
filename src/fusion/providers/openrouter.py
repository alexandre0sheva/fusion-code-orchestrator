"""OpenRouter: open-weight models, and a fallback route for vendor models (Chat Completions).

Every request asks OpenRouter for the billed cost (``usage: {include: true}``), which the ledger
records instead of the catalog estimate, and carries the data policy from the catalog's
``openrouter:`` block: ``data_collection: "allow"`` (providers that store prompts without training
on them are fine), ``require_parameters: true`` and an ``ignore`` list of providers that may train
on prompts. The account-level "no training" setting is the other half of that rule (see
``docs/CONFIGURATION.md``). Only a generic ``X-Title`` is sent, never anything about the user.
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
        return payload

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
