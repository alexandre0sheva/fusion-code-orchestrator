"""Shared HTTP helpers for provider adapters."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from fusion.providers.base import ProviderError

_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 0.25


def extract_error_message(status_code: int, body: str, provider: str) -> str:
    """Build a readable error from an HTTP response body."""
    detail = body[:500] if body else f"HTTP {status_code}"
    try:
        payload = json.loads(body)
        if isinstance(payload, dict):
            for key in ("error", "message", "detail"):
                value = payload.get(key)
                if isinstance(value, str):
                    detail = value
                    break
                if isinstance(value, dict) and "message" in value:
                    detail = str(value["message"])
                    break
    except json.JSONDecodeError:
        pass
    return f"{provider} API error {status_code}: {detail}"


async def post_json_with_retries(
    *,
    client: httpx.AsyncClient,
    url: str,
    provider: str,
    headers: dict[str, str] | None = None,
    json_payload: dict[str, Any] | None = None,
    max_retries: int = _MAX_RETRIES,
) -> dict[str, Any]:
    """POST JSON and retry transient failures with capped exponential backoff."""
    last_error = "unknown error"
    for attempt in range(max_retries):
        try:
            response = await client.post(url, headers=headers, json=json_payload)
        except httpx.TimeoutException as exc:
            last_error = f"{provider} request timed out"
            if attempt + 1 >= max_retries:
                raise ProviderError(last_error) from exc
            await asyncio.sleep(_BACKOFF_BASE_S * (2**attempt))
            continue
        except httpx.HTTPError as exc:
            last_error = f"{provider} HTTP error: {exc}"
            if attempt + 1 >= max_retries:
                raise ProviderError(last_error) from exc
            await asyncio.sleep(_BACKOFF_BASE_S * (2**attempt))
            continue

        if response.status_code in _TRANSIENT_STATUS and attempt + 1 < max_retries:
            await asyncio.sleep(_BACKOFF_BASE_S * (2**attempt))
            continue

        if response.status_code != 200:
            raise ProviderError(
                extract_error_message(response.status_code, response.text, provider)
            )

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"{provider} returned malformed JSON") from exc

        if not isinstance(data, dict):
            raise ProviderError(f"{provider} returned unexpected JSON payload")
        return data

    raise ProviderError(last_error)


def try_parse_json(text: str) -> dict[str, Any] | None:
    """Parse JSON from model text, including fenced blocks."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            parsed = json.loads(stripped[start:end])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None
