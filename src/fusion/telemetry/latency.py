"""Latency tracking utilities."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class LatencyTracker:
    """Tracks latency across pipeline steps."""

    start_time: float = field(default_factory=time.perf_counter)
    step_latencies: dict[str, float] = field(default_factory=dict)

    def record_step(self, name: str, latency_ms: float) -> None:
        self.step_latencies[name] = latency_ms

    @property
    def total_ms(self) -> float:
        return (time.perf_counter() - self.start_time) * 1000
