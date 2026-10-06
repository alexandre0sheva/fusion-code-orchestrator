"""An event loop whose clock is virtual, so simulated studies are instant and exactly repeatable.

Under ``VirtualTimeLoop`` nothing waits in real time: when every task is asleep the loop moves its
clock straight to the next wake-up. A simulated model that "takes 12 seconds" therefore takes none
of ours, parallel calls still overlap on the virtual clock, and timings (``seconds_to_complete``,
time to first token) come out identical on every run. Only code that does no real I/O may run
here: the simulated providers and an in-process SQLite store qualify, a network call or
``asyncio.to_thread`` does not.
"""

from __future__ import annotations

import asyncio
import heapq
from collections.abc import Callable, Coroutine
from typing import Any

__all__ = ["VirtualTimeLoop", "offload", "run_virtual"]


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """A selector loop that jumps to the next timer instead of sleeping until it."""

    def __init__(self) -> None:
        super().__init__()
        self._virtual_now = 0.0

    def time(self) -> float:
        return self._virtual_now

    def _run_once(self) -> None:
        loop: Any = self  # the scheduling queues are asyncio internals
        if not loop._ready:
            scheduled = loop._scheduled
            while scheduled and scheduled[0]._cancelled:
                heapq.heappop(scheduled)._scheduled = False
            if scheduled:
                self._virtual_now = max(self._virtual_now, scheduled[0]._when)
        super()._run_once()  # type: ignore[misc]


def run_virtual[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro`` to completion on a fresh virtual-time loop."""
    with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
        return runner.run(coro)


async def offload[T](fn: Callable[..., T], *args: Any) -> T:
    """Run blocking work (a sandboxed test run) without stalling a real loop.

    On a real loop it goes to a worker thread. On the virtual loop it runs inline: a worker thread
    would let the virtual clock jump ahead while the work is still going, firing every timeout.
    The work takes no virtual time, so a simulated study's timings never depend on how fast this
    machine runs tests.
    """
    if isinstance(asyncio.get_running_loop(), VirtualTimeLoop):
        return fn(*args)
    return await asyncio.to_thread(fn, *args)
