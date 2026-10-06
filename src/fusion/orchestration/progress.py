"""Progress reporting for a run: one message per step ("panel 2/3 done", "synthesizing").

The pipeline never knows who listens. A caller (the MCP server) installs a sink for the duration of
a request with ``progress_sink``; code deep in the pipeline calls ``report``. Without a sink
``report`` does nothing, and a sink that raises never breaks a run. The sink is held in a
``ContextVar``, so concurrent requests each keep their own.

A second channel carries one event per model call, for a live view of who is working and what each
call cost: ``call_observer`` installs an observer, and the call gateway tells it when a call
starts and when its record is on the ledger.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from fusion.orchestration.ledger import CallRecord

__all__ = [
    "CallObserver",
    "ProgressSink",
    "call_finished",
    "call_observer",
    "call_started",
    "progress_sink",
    "report",
]

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


class CallObserver(Protocol):
    """Hears about every model call of a run; must be quick and must not raise."""

    def call_started(self, stage: str, alias: str) -> None: ...

    def call_finished(self, record: CallRecord) -> None: ...


_observer: ContextVar[CallObserver | None] = ContextVar("fusion_call_observer", default=None)


@contextmanager
def call_observer(observer: CallObserver | None) -> Iterator[None]:
    """Tell ``observer`` about every model call made inside the block."""
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def call_started(stage: str, alias: str) -> None:
    observer = _observer.get()
    if observer is None:
        return
    try:
        observer.call_started(stage, alias)
    except Exception as exc:  # a display problem never breaks a run
        print(f"fusion: call observer failed: {exc}", file=sys.stderr)


def call_finished(record: CallRecord) -> None:
    observer = _observer.get()
    if observer is None:
        return
    try:
        observer.call_finished(record)
    except Exception as exc:
        print(f"fusion: call observer failed: {exc}", file=sys.stderr)
