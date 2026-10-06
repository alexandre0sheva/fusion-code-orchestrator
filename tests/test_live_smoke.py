"""Live smoke tests: each provider adapter against the real API. These cost money.

Skipped by default (``addopts`` deselects the ``live`` marker). Run deliberately, with keys set:

    uv run pytest -m live

Each test asks one short question (a few hundred output tokens at most, about a cent), checks the
call against the roadmap's live-spend ledger first and records what it spent afterwards. A test
skips when its provider's key is not set. They check what the offline tests cannot: that the real
API still returns the shapes the recorded fixtures in ``tests/fixtures/providers`` describe.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from fusion.bench.spend import default_ledger
from fusion.config.catalog import load_catalog
from fusion.providers.anthropic import AnthropicProvider
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.providers.google import GoogleProvider
from fusion.providers.openai import OpenAIProvider
from fusion.telemetry.cost import PricingRegistry

pytestmark = pytest.mark.live

CASES: dict[str, tuple[type[ModelProvider], str, str]] = {
    # provider: (adapter, key variable, catalog alias)
    "anthropic": (AnthropicProvider, "ANTHROPIC_API_KEY", "claude-haiku"),
    "openai": (OpenAIProvider, "OPENAI_API_KEY", "gpt-luna"),
    "google": (GoogleProvider, "GOOGLE_API_KEY", "gemini-flash"),
}
# The keys and the ledger are read when the module is imported: the autouse fixtures in conftest
# remove provider keys and point the project directory at a temporary one for every test, which
# would hide the keys and make the spend ledger forget what was spent.
KEYS = {name: os.environ.get(var, "") for name, (_, var, _) in CASES.items()}
LEDGER = default_ledger()

ESTIMATE_USD = 0.02  # a generous bound for one short call
PROMPT = "Reply with the single word: pong"
# What every successful response of a provider carries, from the recorded fixtures.
RESPONSE_KEYS = {
    "anthropic": {"id", "type", "role", "model", "content", "stop_reason", "usage"},
    "openai": {"id", "object", "model", "choices", "usage"},
    "google": {"candidates", "usageMetadata", "modelVersion"},
}
USAGE_KEYS = {
    "anthropic": ("usage", {"input_tokens", "output_tokens"}),
    "openai": ("usage", {"prompt_tokens", "completion_tokens"}),
    "google": ("usageMetadata", {"promptTokenCount", "candidatesTokenCount"}),
}


async def ask(name: str, *, stream: bool = False) -> ModelResponse:
    adapter, variable, alias = CASES[name]
    if not KEYS[name]:
        pytest.skip(f"{variable} is not set")
    entry = load_catalog().models[alias]
    LEDGER.check(ESTIMATE_USD, f"live smoke test of {name}")
    provider = adapter(api_key=KEYS[name])
    try:
        response = await provider.safe_complete(
            ModelRequest(model_id=entry.model_id, user_prompt=PROMPT, max_tokens=512, stream=stream)
        )
    finally:
        await provider.aclose()
    cost = PricingRegistry().estimate_response_cost(response, entry).amount_usd
    LEDGER.append(cost or ESTIMATE_USD, task="task-25", purpose=f"live smoke test of {name}")
    return response


@pytest.mark.parametrize("name", CASES)
async def test_a_tiny_prompt_is_answered_and_usage_is_reported(name: str) -> None:
    response = await ask(name)
    assert response.ok, response.error
    assert "pong" in response.text.lower()
    assert (response.input_tokens or 0) > 0 and (response.output_tokens or 0) > 0
    assert response.latency_ms > 0 and response.finish_reason


@pytest.mark.parametrize("name", CASES)
async def test_the_real_response_still_has_the_shape_the_fixtures_record(name: str) -> None:
    response = await ask(name)
    assert response.ok, response.error
    raw: dict[str, Any] = response.raw_response or {}
    assert RESPONSE_KEYS[name] <= set(raw), f"missing {RESPONSE_KEYS[name] - set(raw)}"
    field, expected = USAGE_KEYS[name]
    assert expected <= set(raw[field]), f"{name} usage lost {expected - set(raw[field])}"


@pytest.mark.parametrize("name", CASES)
async def test_streaming_delivers_the_text_and_a_time_to_first_token(name: str) -> None:
    response = await ask(name, stream=True)
    assert response.ok, response.error
    assert "pong" in response.text.lower()
    assert response.ttft_ms is not None and response.ttft_ms > 0
