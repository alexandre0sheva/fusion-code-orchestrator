"""Async utility helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from typing import TypeVar

T = TypeVar("T")


async def gather_with_errors(
    coros: Sequence[Awaitable[T]],
) -> list[T | BaseException]:
    """Run coroutines concurrently, returning results or exceptions."""
    results = await asyncio.gather(*coros, return_exceptions=True)
    return list(results)
