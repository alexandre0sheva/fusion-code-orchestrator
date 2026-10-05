"""Provider-response cache for studies: re-running costs nothing for calls already paid for.

``CachingProvider`` wraps a real provider. A request that was answered before (same provider,
same request fields, including the model, prompt, schema, temperature and seed) is replayed from
disk and flagged ``cache_hit``: it is billed at zero, and keeps the latency and speed it had when
it was first answered. Only successful answers are stored. Repeats of a study differ in their
seed, so they never share cache entries with each other, only with a rerun of the same repeat.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse

__all__ = ["CachingProvider", "ResponseDiskCache", "request_digest"]

_UNCACHED_FIELDS = {"timeout"}  # how long to wait never changes the answer


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest()}
    msg = f"not serialisable: {type(value).__name__}"
    raise TypeError(msg)


def request_digest(provider: str, request: ModelRequest) -> str:
    """Stable hash of everything that decides what a provider answers."""
    payload = {
        "provider": provider,
        "request": request.model_dump(mode="python", exclude=_UNCACHED_FIELDS),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_jsonable)
    return hashlib.sha256(blob.encode()).hexdigest()


class ResponseDiskCache:
    """One JSON file per answer, under ``root/<first two hex digits>/``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> ModelResponse | None:
        path = self._path(key)
        try:
            response = ModelResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):  # absent, or damaged: treat as a miss
            self.misses += 1
            return None
        self.hits += 1
        return response.model_copy(update={"cache_hit": True})

    def put(self, key: str, response: ModelResponse) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        stored = response.model_copy(update={"raw_response": None, "cache_hit": False})
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(stored.model_dump_json(), encoding="utf-8")
        os.replace(tmp, path)


class CachingProvider(ModelProvider):
    """A provider that answers from the cache when it can and records what it had to ask."""

    def __init__(self, inner: ModelProvider, cache: ResponseDiskCache) -> None:
        self.inner = inner
        self.cache = cache
        self.name = inner.name

    def is_available(self) -> bool:
        return self.inner.is_available()

    async def aclose(self) -> None:
        await self.inner.aclose()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        key = request_digest(self.name, request)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        response = await self.inner.safe_complete(request)
        if response.ok:
            self.cache.put(key, response)
        return response
