"""LM Studio OpenAI-compatible local provider."""

from __future__ import annotations

import os
import time
from collections.abc import Callable

import httpx

from fusion.config.catalog import Catalog
from fusion.config.env import is_local_provider_enabled
from fusion.providers.http_utils import RetryPolicy
from fusion.providers.limits import ProviderLimiter
from fusion.providers.openai import OpenAICompatibleProvider


class LMStudioProvider(OpenAICompatibleProvider):
    """Calls LM Studio via its OpenAI-compatible API (streaming is not used locally)."""

    name = "lmstudio"

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
        default = "http://localhost:1234/v1"
        self._base_url = (base_url or os.environ.get("LMSTUDIO_BASE_URL", default)).rstrip("/")
        self._unreachable = f"LM Studio not reachable at {self._base_url}"
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

    def _chat_url(self) -> str:
        return f"{self._base_url}/chat/completions"
