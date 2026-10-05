"""Per-provider concurrency and request-rate limits."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fusion.config.catalog import Catalog

_WINDOW_S = 60.0


class ProviderLimiter:
    """Caps in-flight requests (semaphore) and request starts per minute (sliding window).

    Both limits are optional; a limiter with neither never waits. One limiter is shared by all
    calls to a provider, so parallel panel members cannot burst past the provider's quota.
    """

    def __init__(
        self,
        max_concurrent: int | None = None,
        rpm: int | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.rpm = rpm
        self._clock = clock
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent else None
        self._starts: deque[float] = deque()
        self._rate_lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one request slot for the duration of the ``async with`` block."""
        if self._semaphore is not None:
            await self._semaphore.acquire()
        try:
            await self._wait_for_rate_window()
            yield
        finally:
            if self._semaphore is not None:
                self._semaphore.release()

    async def _wait_for_rate_window(self) -> None:
        if not self.rpm:
            return
        async with self._rate_lock:
            while True:
                now = self._clock()
                while self._starts and now - self._starts[0] >= _WINDOW_S:
                    self._starts.popleft()
                if len(self._starts) < self.rpm:
                    self._starts.append(now)
                    return
                await self._sleep(_WINDOW_S - (now - self._starts[0]))


def build_limiters(catalog: Catalog) -> dict[str, ProviderLimiter]:
    """One limiter per provider that declares limits in the catalog."""
    return {
        provider: ProviderLimiter(limits.max_concurrent, limits.rpm)
        for provider, limits in catalog.provider_limits.items()
    }
