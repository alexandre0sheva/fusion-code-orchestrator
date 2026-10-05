"""HTTP layer: retries, error taxonomy, shared client lifecycle, rate limiting."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from _provider_helpers import (
    FakeClock,
    FakeSleep,
    always,
    openai_ok,
    sequence,
)
from fusion.providers.base import ModelRequest
from fusion.providers.http_utils import RetryPolicy
from fusion.providers.limits import ProviderLimiter
from fusion.providers.openai import OpenAIProvider

REQ = ModelRequest(model_id="gpt-6-luna", user_prompt="hello")


def _provider(
    transport: httpx.AsyncBaseTransport,
    sleep: FakeSleep,
    *,
    jitter: float = 1.0,
    limiter: ProviderLimiter | None = None,
) -> OpenAIProvider:
    return OpenAIProvider(
        api_key="sk-test-secret-123456789",
        transport=transport,
        retry_policy=RetryPolicy(sleep=sleep, jitter=lambda: jitter),
        limiter=limiter,
    )


async def test_429_with_retry_after_waits_then_succeeds() -> None:
    sleep = FakeSleep()
    transport, seen = sequence(httpx.Response(429, headers={"Retry-After": "2"}), openai_ok())
    response = await _provider(transport, sleep).safe_complete(REQ)
    assert response.ok
    assert sleep.calls == [2.0]
    assert response.retries == 1
    assert len(seen) == 2


async def test_backoff_is_full_jitter_and_capped_by_exponential() -> None:
    sleep = FakeSleep()
    transport, _ = sequence(httpx.Response(503), httpx.Response(503), openai_ok())
    response = await _provider(transport, sleep, jitter=1.0).safe_complete(REQ)
    assert response.ok
    assert sleep.calls == [0.25, 0.5]


async def test_full_jitter_scales_delay_down() -> None:
    sleep = FakeSleep()
    transport, _ = sequence(httpx.Response(500), openai_ok())
    await _provider(transport, sleep, jitter=0.5).safe_complete(REQ)
    assert sleep.calls == [0.125]


async def test_400_is_never_retried() -> None:
    sleep = FakeSleep()
    transport, seen = always(httpx.Response(400, json={"error": {"message": "bad schema"}}))
    response = await _provider(transport, sleep).safe_complete(REQ)
    assert not response.ok
    assert response.error_type == "BadRequest"
    assert "bad schema" in (response.error or "")
    assert len(seen) == 1
    assert sleep.calls == []
    assert response.retries == 0


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors_are_not_retried(status: int) -> None:
    sleep = FakeSleep()
    transport, seen = always(httpx.Response(status, json={"error": {"message": "nope"}}))
    response = await _provider(transport, sleep).safe_complete(REQ)
    assert response.error_type == "Auth"
    assert len(seen) == 1


async def test_rate_limit_exhaustion_reports_rate_limit() -> None:
    sleep = FakeSleep()
    transport, seen = always(httpx.Response(429))
    response = await _provider(transport, sleep).safe_complete(REQ)
    assert response.error_type == "RateLimit"
    assert len(seen) == 3
    assert response.retries == 2


async def test_server_error_exhaustion_reports_server() -> None:
    sleep = FakeSleep()
    transport, _ = always(httpx.Response(502))
    response = await _provider(transport, sleep).safe_complete(REQ)
    assert response.error_type == "Server"


async def test_timeouts_are_retried_and_reported() -> None:
    sleep = FakeSleep()
    transport, seen = sequence(httpx.ReadTimeout("slow"))
    response = await _provider(transport, sleep).safe_complete(REQ)
    assert response.error_type == "Timeout"
    assert len(seen) == 3
    assert response.retries == 2


async def test_retry_after_beyond_cap_is_not_waited_for() -> None:
    sleep = FakeSleep()
    transport, seen = always(httpx.Response(429, headers={"Retry-After": "3600"}))
    response = await _provider(transport, sleep).safe_complete(REQ)
    assert response.error_type == "RateLimit"
    assert len(seen) == 1
    assert sleep.calls == []


async def test_api_key_is_redacted_from_error_text() -> None:
    sleep = FakeSleep()
    secret = "sk-test-secret-123456789"
    transport, _ = always(
        httpx.Response(400, json={"error": {"message": f"bad key {secret} supplied"}})
    )
    response = await _provider(transport, sleep).safe_complete(REQ)
    assert secret not in (response.error or "")
    assert "[redacted]" in (response.error or "")


async def test_http_client_is_shared_across_calls_and_closed_by_aclose() -> None:
    transport, _ = always(openai_ok())
    provider = _provider(transport, FakeSleep())
    await provider.safe_complete(REQ)
    first = provider.http_client
    await provider.safe_complete(REQ)
    assert provider.http_client is first
    assert not first.is_closed
    await provider.aclose()
    assert first.is_closed
    # A closed provider can be used again: the client is re-created lazily.
    again = await provider.safe_complete(REQ)
    assert again.ok
    assert provider.http_client is not first
    await provider.aclose()


async def test_limiter_caps_in_flight_requests() -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return openai_ok()

    provider = _provider(
        httpx.MockTransport(handler), FakeSleep(), limiter=ProviderLimiter(max_concurrent=2)
    )
    results = await asyncio.gather(*(provider.safe_complete(REQ) for _ in range(6)))
    assert all(r.ok for r in results)
    assert peak == 2


async def test_rpm_limiter_waits_for_the_window_to_free_up() -> None:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    limiter = ProviderLimiter(rpm=2, clock=clock, sleep=sleep)
    async with limiter.slot():
        pass
    async with limiter.slot():
        pass
    assert sleep.calls == []
    async with limiter.slot():
        pass
    assert sleep.calls == [60.0]
    assert clock.now == 60.0


async def test_unlimited_limiter_never_waits() -> None:
    clock = FakeClock()
    sleep = FakeSleep(clock)
    limiter = ProviderLimiter(clock=clock, sleep=sleep)
    for _ in range(50):
        async with limiter.slot():
            pass
    assert sleep.calls == []
