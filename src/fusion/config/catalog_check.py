"""Check catalog model IDs against each provider's list-models endpoint (free calls)."""

from __future__ import annotations

from typing import Any, Literal

import httpx
from pydantic import BaseModel

from fusion.config.catalog import FREE_PROVIDERS, Catalog

CheckStatus = Literal["ok", "unknown_id", "skipped", "error"]

_TIMEOUT_SECONDS = 20.0
_MAX_PAGES = 20


class ModelCheck(BaseModel):
    """Result of checking one catalog model."""

    alias: str
    provider: str
    model_id: str
    status: CheckStatus
    detail: str = ""


async def _get_json(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> Any:
    response = await client.get(url, headers=headers, timeout=_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


async def _anthropic_ids(client: httpx.AsyncClient, api_key: str) -> set[str]:
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    ids: set[str] = set()
    after: str | None = None
    for _ in range(_MAX_PAGES):
        url = "https://api.anthropic.com/v1/models?limit=1000"
        if after:
            url += f"&after_id={after}"
        data = await _get_json(client, url, headers)
        ids.update(str(item["id"]) for item in data.get("data", []))
        if not data.get("has_more") or not data.get("last_id"):
            break
        after = str(data["last_id"])
    return ids


async def _openai_ids(client: httpx.AsyncClient, api_key: str) -> set[str]:
    data = await _get_json(
        client, "https://api.openai.com/v1/models", {"Authorization": f"Bearer {api_key}"}
    )
    return {str(item["id"]) for item in data.get("data", [])}


async def _google_ids(client: httpx.AsyncClient, api_key: str) -> set[str]:
    headers = {"x-goog-api-key": api_key}
    ids: set[str] = set()
    token: str | None = None
    for _ in range(_MAX_PAGES):
        url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
        if token:
            url += f"&pageToken={token}"
        data = await _get_json(client, url, headers)
        ids.update(str(item["name"]).removeprefix("models/") for item in data.get("models", []))
        token = data.get("nextPageToken")
        if not token:
            break
    return ids


_FETCHERS = {"anthropic": _anthropic_ids, "openai": _openai_ids, "google": _google_ids}


def _id_is_listed(model_id: str, listed: set[str]) -> bool:
    # A dateless alias (claude-haiku-4-5) may be listed only as its dated snapshot.
    return model_id in listed or any(item.startswith(f"{model_id}-") for item in listed)


async def check_catalog_live(
    catalog: Catalog,
    api_keys: dict[str, str],
    client: httpx.AsyncClient,
) -> list[ModelCheck]:
    """Compare enabled catalog models with what each provider reports as available.

    Only the free list-models endpoints are called; keys are sent in headers and never logged.
    """
    listed: dict[str, set[str] | str] = {}
    for provider, fetch in _FETCHERS.items():
        key = api_keys.get(provider)
        if not key:
            continue
        try:
            listed[provider] = await fetch(client, key)
        except httpx.HTTPError as exc:
            listed[provider] = f"{type(exc).__name__}: could not list models"

    results: list[ModelCheck] = []
    for alias, entry in catalog.models.items():
        if not entry.enabled or entry.provider in FREE_PROVIDERS:
            continue
        base = {"alias": alias, "provider": entry.provider, "model_id": entry.model_id}
        provider_result = listed.get(entry.provider)
        if provider_result is None:
            detail = f"no API key for {entry.provider}"
            results.append(ModelCheck(**base, status="skipped", detail=detail))
        elif isinstance(provider_result, str):
            results.append(ModelCheck(**base, status="error", detail=provider_result))
        elif _id_is_listed(entry.model_id, provider_result):
            results.append(ModelCheck(**base, status="ok"))
        else:
            detail = "provider does not list this model ID"
            results.append(ModelCheck(**base, status="unknown_id", detail=detail))
    return results
