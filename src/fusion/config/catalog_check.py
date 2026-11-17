"""Check catalog model IDs and prices against what providers list (free calls only)."""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from fusion.config.catalog import (
    FREE_PROVIDERS,
    Catalog,
    ModelEntry,
    PriceSchedule,
    newer_in_family,
)
from fusion.providers.anthropic import API_VERSION

# newer: the ID is listed but the provider also lists a newer model of the same family.
# redirect: the price's source_url answers with a redirect (the page moved; re-verify there).
# price_drift: OpenRouter's list price differs from the catalog's by more than 5%.
# policy: OpenRouter marks a provider as training on prompts and the catalog does not ignore it.
CheckStatus = Literal[
    "ok", "unknown_id", "skipped", "error", "newer", "redirect", "price_drift", "policy"
]

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
    headers = {"x-api-key": api_key, "anthropic-version": API_VERSION}
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


async def _redirects(client: httpx.AsyncClient, url: str) -> str | None:
    """Where ``url`` redirects to, or None (also when the page cannot be fetched)."""
    try:
        response = await client.get(url, follow_redirects=False, timeout=_TIMEOUT_SECONDS)
    except httpx.HTTPError:
        return None
    if response.is_redirect:
        return response.headers.get("location") or "another address"
    return None


async def _source_checks(catalog: Catalog, client: httpx.AsyncClient) -> list[ModelCheck]:
    seen: dict[str, str | None] = {}
    results: list[ModelCheck] = []
    for alias, entry in catalog.models.items():
        if not entry.enabled or entry.provider in FREE_PROVIDERS:
            continue
        for url in dict.fromkeys(p.source_url for p in entry.prices if p.source_url):
            if url not in seen:
                seen[url] = await _redirects(client, url)
            target = seen[url]
            if target is not None:
                results.append(
                    ModelCheck(
                        alias=alias,
                        provider=entry.provider,
                        model_id=entry.model_id,
                        status="redirect",
                        detail=f"source_url {url} redirects to {target}; update it",
                    )
                )
    return results


async def check_catalog_live(
    catalog: Catalog,
    api_keys: dict[str, str],
    client: httpx.AsyncClient,
) -> list[ModelCheck]:
    """Compare enabled catalog models with what each provider reports as available.

    Only free endpoints are called: each provider's list-models endpoint (keys are sent in headers
    and never logged) and each price's ``source_url`` (to catch pages that moved).
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
        # OpenRouter's own models are checked by ``check_openrouter_live``.
        if not entry.enabled or entry.provider in FREE_PROVIDERS or entry.provider == "openrouter":
            continue
        base = {"alias": alias, "provider": entry.provider, "model_id": entry.model_id}
        provider_result = listed.get(entry.provider)
        if provider_result is None:
            detail = f"no API key for {entry.provider}"
            results.append(ModelCheck(**base, status="skipped", detail=detail))
        elif isinstance(provider_result, str):
            results.append(ModelCheck(**base, status="error", detail=provider_result))
        elif not _id_is_listed(entry.model_id, provider_result):
            detail = "provider does not list this model ID"
            results.append(ModelCheck(**base, status="unknown_id", detail=detail))
        elif newer := newer_in_family(entry.provider, entry.model_id, provider_result):
            detail = f"the provider lists {', '.join(newer)}, newer than this model"
            results.append(ModelCheck(**base, status="newer", detail=detail))
        else:
            results.append(ModelCheck(**base, status="ok"))
    results.extend(await _source_checks(catalog, client))
    return results


# -- OpenRouter (no key needed) ------------------------------------------------------------------

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
# The list behind OpenRouter's provider-logging docs page; the only place that says, per provider,
# whether it may train on prompts (``dataPolicy.training``). Undocumented, so a failure to read it
# is reported as an error, never as a pass.
OPENROUTER_PROVIDERS_URL = "https://openrouter.ai/api/frontend/v1/all-providers"
PRICE_DRIFT_TOLERANCE = 0.05
POLICY_ALIAS = "openrouter-privacy"

_SNAPSHOT_DATE = re.compile(r"-(?:\d{4}|\d{8})$")
_VERSION = re.compile(r"\d+(?:\.\d+)*")

# (above_prompt_tokens or None, input per 1M, output per 1M)
_PriceRow = tuple[int | None, float, float]


def openrouter_family(model_id: str) -> tuple[str, tuple[int, ...]] | None:
    """``(family, version)`` of an OpenRouter ID; None for variants (``:batch``) and ``~`` aliases.

    The family is the ID with its snapshot date dropped and each version number replaced by ``#``,
    so ``deepseek/deepseek-v4.1-flash-1017`` is ``deepseek/deepseek-v#-flash`` version ``(4, 1)``
    and ``qwen/qwen3.8-omni-flash`` is another family than ``qwen/qwen3.8-flash``.
    """
    if ":" in model_id or model_id.startswith("~"):
        return None
    base = _SNAPSHOT_DATE.sub("", model_id)
    version = tuple(int(part) for number in _VERSION.findall(base) for part in number.split("."))
    return _VERSION.sub("#", base), version


def _per_million(value: Any) -> float | None:
    try:
        return float(value) * 1_000_000
    except (TypeError, ValueError):
        return None


async def fetch_openrouter_models(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    """OpenRouter's public model list keyed by ID (no key needed)."""
    data = await _get_json(client, OPENROUTER_MODELS_URL, {})
    return {str(item["id"]): item for item in data.get("data", []) if "id" in item}


async def fetch_openrouter_prices(client: httpx.AsyncClient) -> dict[str, tuple[float, float]]:
    """``{id: (input, output)}`` USD per 1M tokens from OpenRouter's public list (no key)."""
    prices: dict[str, tuple[float, float]] = {}
    for model_id, item in (await fetch_openrouter_models(client)).items():
        pricing = item.get("pricing") or {}
        prompt = _per_million(pricing.get("prompt"))
        completion = _per_million(pricing.get("completion"))
        if prompt is not None and completion is not None:
            prices[model_id] = (prompt, completion)
    return prices


async def fetch_training_providers(client: httpx.AsyncClient) -> dict[str, str]:
    """``{slug: display name}`` of OpenRouter providers that may train on prompts."""
    data = await _get_json(client, OPENROUTER_PROVIDERS_URL, {})
    return {
        str(item.get("slug") or item.get("name")): str(item.get("displayName") or item.get("slug"))
        for item in data.get("data", [])
        if (item.get("dataPolicy") or {}).get("training")
    }


def _openrouter_rows(item: dict[str, Any]) -> list[_PriceRow]:
    pricing = item.get("pricing") or {}
    rows: list[_PriceRow] = []
    for row in [pricing, *(pricing.get("overrides") or [])]:
        prompt = _per_million(row.get("prompt", pricing.get("prompt")))
        completion = _per_million(row.get("completion", pricing.get("completion")))
        if prompt is not None and completion is not None:
            rows.append((row.get("min_prompt_tokens"), prompt, completion))
    return rows


def _drift(catalog_price: float, listed: float) -> float:
    if catalog_price == 0:
        return 0.0 if listed == 0 else float("inf")
    return abs(listed - catalog_price) / catalog_price


def _price_drift(entry: ModelEntry, item: dict[str, Any], day: date) -> str | None:
    """Why the catalog's prices in effect on ``day`` disagree with OpenRouter's, or None."""
    rows = _openrouter_rows(item)
    schedules: list[PriceSchedule] = [p for p in entry.prices if p.covers(day)]
    problems: list[str] = []
    for schedule in schedules:
        candidates = [r for r in rows if r[0] == schedule.above_prompt_tokens]
        if not candidates:
            continue
        best = min(
            candidates,
            key=lambda r: max(
                _drift(schedule.input_per_1m, r[1]), _drift(schedule.output_per_1m, r[2])
            ),
        )
        drift = max(
            _drift(schedule.input_per_1m, best[1]), _drift(schedule.output_per_1m, best[2])
        )
        if drift > PRICE_DRIFT_TOLERANCE:
            where = (
                f" above {schedule.above_prompt_tokens:,} prompt tokens"
                if schedule.above_prompt_tokens
                else ""
            )
            problems.append(
                f"OpenRouter lists ${best[1]:.4g} / ${best[2]:.4g}{where}, the catalog "
                f"${schedule.input_per_1m:g} / ${schedule.output_per_1m:g} ({drift:.0%} apart)"
            )
    known = {s.above_prompt_tokens for s in schedules}
    for threshold, prompt, completion in rows:
        if threshold is not None and threshold not in known:
            problems.append(
                f"OpenRouter charges ${prompt:.4g} / ${completion:.4g} above {threshold:,} prompt "
                "tokens and the catalog has no such row"
            )
    return "; ".join(problems) or None


def _newer_on_openrouter(model_id: str, listed: dict[str, dict[str, Any]]) -> list[str]:
    own = openrouter_family(model_id)
    created = listed[model_id].get("created") or 0
    if own is None:
        return []
    newer: list[tuple[int, str]] = []
    for other_id, item in listed.items():
        other = openrouter_family(other_id)
        if (
            other_id != model_id
            and other is not None
            and other[0] == own[0]
            and other[1] >= own[1]
            and (item.get("created") or 0) > created
        ):
            newer.append((int(item.get("created") or 0), other_id))
    return [other_id for _, other_id in sorted(newer, reverse=True)]


async def _policy_check(catalog: Catalog, client: httpx.AsyncClient) -> ModelCheck:
    base = {"alias": POLICY_ALIAS, "provider": "openrouter", "model_id": ""}
    try:
        training = await fetch_training_providers(client)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        detail = (
            f"{type(exc).__name__}: could not read OpenRouter's provider data policies; check "
            f"{catalog.openrouter.source_url or 'the provider-logging page'} by hand"
        )
        return ModelCheck(**base, status="error", detail=detail)
    missing = sorted(set(training) - set(catalog.openrouter.provider_ignore))
    if missing:
        names = ", ".join(f"{slug} ({training[slug]})" for slug in missing)
        detail = f"may train on prompts and are not in openrouter.provider_ignore: {names}"
        return ModelCheck(**base, status="policy", detail=detail)
    detail = f"provider_ignore covers all {len(training)} providers that may train on prompts"
    return ModelCheck(**base, status="ok", detail=detail)


def _check_listed(
    entry: ModelEntry, alias: str, listed: dict[str, dict[str, Any]], day: date
) -> ModelCheck:
    model_id = entry.openrouter_id or ""
    base = {"alias": alias, "provider": "openrouter", "model_id": model_id}
    if model_id not in listed:
        return ModelCheck(**base, status="unknown_id", detail="OpenRouter does not list this ID")
    if drift := _price_drift(entry, listed[model_id], day):
        return ModelCheck(**base, status="price_drift", detail=drift)
    if newer := _newer_on_openrouter(model_id, listed):
        detail = f"OpenRouter lists {', '.join(newer)}, newer than this model"
        return ModelCheck(**base, status="newer", detail=detail)
    return ModelCheck(**base, status="ok")


async def check_openrouter_live(
    catalog: Catalog, client: httpx.AsyncClient, *, today: date | None = None
) -> list[ModelCheck]:
    """Every enabled model with an OpenRouter ID against OpenRouter's public list, plus the policy.

    Per model: the ID is listed, prices in effect today are within 5% of OpenRouter's (long-context
    and time-of-day rows included), and no newer model of the same family is listed. Then: every
    provider OpenRouter marks as training on prompts is in ``openrouter.provider_ignore``. Free
    endpoints only; no key is sent.
    """
    day = today or date.today()
    entries = [
        (alias, entry)
        for alias, entry in catalog.models.items()
        if entry.enabled and entry.openrouter_id
    ]
    results: list[ModelCheck] = []
    try:
        listed = await fetch_openrouter_models(client) if entries else {}
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        detail = f"{type(exc).__name__}: could not list OpenRouter models"
        for alias, entry in entries:
            model_id = entry.openrouter_id or ""
            results.append(
                ModelCheck(
                    alias=alias,
                    provider="openrouter",
                    model_id=model_id,
                    status="error",
                    detail=detail,
                )
            )
    else:
        results.extend(_check_listed(entry, alias, listed, day) for alias, entry in entries)
    results.append(await _policy_check(catalog, client))
    return results
