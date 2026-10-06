"""Progress reporting for a run: one message per step ("panel 2/3 done", "synthesizing").

The pipeline never knows who listens. A caller (the MCP server) installs a sink for the duration of
a request with ``progress_sink``; code deep in the pipeline calls ``report``. Without a sink
``report`` does nothing, and a sink that raises never breaks a run. The sink is held in a
``ContextVar``, so concurrent requests each keep their own.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = ["ProgressSink", "progress_sink", "report"]

ProgressSink = Callable[[str], Awaitable[None]]

_sink: ContextVar[ProgressSink | None] = ContextVar("fusion_progress_sink", default=None)


@contextmanager
def progress_sink(sink: ProgressSink | None) -> Iterator[None]:
    """Send every ``report`` made inside the block to ``sink``."""
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


async def report(message: str) -> None:
    """Tell the current listener, if any, what the run is doing now."""
    sink = _sink.get()
    if sink is None:
        return
    try:
        await sink(message)
    except Exception as exc:  # progress is best-effort; stdout is the JSON-RPC channel
        print(f"fusion: progress report failed: {exc}", file=sys.stderr)
