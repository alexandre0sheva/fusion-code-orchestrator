"""Tests for provider error handling."""

from __future__ import annotations

import pytest

from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse, ProviderError
from fusion.providers.mock import MockProvider


class FailingProvider(ModelProvider):
    name = "failing"

    def is_available(self) -> bool:
        return True

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise ProviderError("simulated provider failure")


class RaisingProvider(ModelProvider):
    name = "raising"

    def is_available(self) -> bool:
        return True

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise RuntimeError("unexpected crash")


@pytest.mark.asyncio
async def test_safe_complete_returns_structured_provider_error() -> None:
    provider = FailingProvider()
    response = await provider.safe_complete(
        ModelRequest(model_id="test-model", user_prompt="hello")
    )
    assert response.error == "simulated provider failure"
    assert response.text == ""
    assert not response.ok


@pytest.mark.asyncio
async def test_safe_complete_catches_unexpected_errors() -> None:
    provider = RaisingProvider()
    response = await provider.safe_complete(
        ModelRequest(model_id="test-model", user_prompt="hello")
    )
    assert response.error is not None
    assert "unexpected" in response.error.lower()


@pytest.mark.asyncio
async def test_anthropic_missing_api_key_structured() -> None:
    from fusion.providers.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key="")
    response = await provider.safe_complete(
        ModelRequest(model_id="claude-sonnet", user_prompt="hi")
    )
    assert response.error is not None
    assert "ANTHROPIC_API_KEY" in response.error


@pytest.mark.asyncio
async def test_openai_missing_api_key_structured() -> None:
    from fusion.providers.openai import OpenAIProvider

    provider = OpenAIProvider(api_key="")
    response = await provider.safe_complete(
        ModelRequest(model_id="gpt-5.4-mini", user_prompt="hi")
    )
    assert response.error is not None
    assert "OPENAI_API_KEY" in response.error


@pytest.mark.asyncio
async def test_mock_provider_success_path() -> None:
    provider = MockProvider(latency_ms=0.0)
    response = await provider.safe_complete(
        ModelRequest(model_id="mock-fast", user_prompt="test")
    )
    assert response.ok
    assert response.error is None
