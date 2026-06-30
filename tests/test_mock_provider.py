"""Tests for mock provider."""

import pytest

from fusion.providers.base import ModelRequest
from fusion.providers.mock import MOCK_PERSONALITIES, MockProvider


@pytest.mark.asyncio
async def test_mock_provider_complete() -> None:
    provider = MockProvider(latency_ms=1.0)
    assert provider.is_available()
    request = ModelRequest(
        model_id="mock-fast",
        user_prompt="Review this diff",
        metadata={"task_type": "code_review", "personality": "coding_reviewer"},
    )
    response = await provider.complete(request)
    assert response.provider == "mock"
    assert response.ok
    assert "Review" in response.text or "mock" in response.text.lower()
    assert response.input_tokens and response.input_tokens > 0
    assert response.output_tokens and response.output_tokens > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("personality", sorted(MOCK_PERSONALITIES))
async def test_mock_personalities_deterministic(personality: str) -> None:
    provider = MockProvider(latency_ms=0.0)
    request = ModelRequest(
        model_id=f"mock-{personality}",
        user_prompt="Analyze input payload",
        metadata={"personality": personality, "task_type": "code_review"},
    )
    first = await provider.complete(request)
    second = await provider.complete(request)
    assert first.text == second.text
    assert first.ok


@pytest.mark.asyncio
async def test_mock_judge_response() -> None:
    provider = MockProvider(latency_ms=1.0)
    request = ModelRequest(
        model_id="mock-judge",
        user_prompt="Evaluate response",
        metadata={"role": "judge", "personality": "judge", "task_type": "answer_eval"},
        json_mode=True,
    )
    response = await provider.complete(request)
    assert response.parsed_json is not None
    assert "specificity" in response.parsed_json


@pytest.mark.asyncio
async def test_mock_synthesizer_response() -> None:
    provider = MockProvider(latency_ms=1.0)
    request = ModelRequest(
        model_id="mock-judge",
        user_prompt="Synthesize",
        metadata={
            "role": "synthesizer",
            "personality": "synthesizer",
            "task_type": "implementation_plan",
        },
    )
    response = await provider.complete(request)
    assert response.parsed_json is not None
    assert "implementation_sequence" in response.parsed_json


@pytest.mark.asyncio
async def test_mock_security_reviewer() -> None:
    provider = MockProvider(latency_ms=0.0)
    request = ModelRequest(
        model_id="mock-security",
        user_prompt="Review auth code",
        metadata={"personality": "security_reviewer"},
    )
    response = await provider.complete(request)
    assert "Security" in response.text


@pytest.mark.asyncio
async def test_mock_weak_model() -> None:
    provider = MockProvider(latency_ms=0.0)
    request = ModelRequest(
        model_id="mock-weak",
        user_prompt="Review",
        metadata={"personality": "weak_model"},
    )
    response = await provider.complete(request)
    assert len(response.text.split()) < 10
