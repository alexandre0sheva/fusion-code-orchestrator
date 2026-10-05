"""Optional cache of whole answers for identical requests (real mode only, off by default).

An MCP server often sees the same question twice: a retried tool call, or two agents asking about
the same diff. When ``cache.enabled`` is set, a run whose redacted task text, task type,
strategy and model limit match an earlier run that completed less than ``cache.ttl_seconds``
ago returns that run's answer without calling any model. The cache lives in the memory of one
server process; nothing is written to disk, a halted run is never cached, and benchmark mode
never reads or writes it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable

from fusion.config.loader import CacheConfig
from fusion.orchestration.context import PipelineContext
from fusion.orchestration.ledger import RunLedger
from fusion.orchestration.result import PipelineResult
from fusion.orchestration.strategy import Strategy
from fusion.security.redaction import redact_secrets
from fusion.telemetry.cost import CostComparison, UsageSummary

__all__ = ["ResponseCache", "request_key"]


def request_key(ctx: PipelineContext, strategy: Strategy) -> str:
    """Stable key over everything that decides the answer: the sanitized prompt and the strategy.

    The strategy's whole definition is part of the key, so editing a strategy or its models
    never serves an answer produced by the old one.
    """
    payload = {
        "task_type": ctx.task_type.value,
        "primary": redact_secrets(ctx.primary_content).text,
        "context": redact_secrets(ctx.context).text,
        "snippets": [redact_secrets(s).text for s in ctx.file_snippets],
        "changed_files": ctx.changed_files,
        "max_models": ctx.max_models,
        "strategy": strategy.model_dump(mode="json"),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def _cached_view(result: PipelineResult, *, age_s: float, lookup_ms: float) -> PipelineResult:
    """The stored answer as a new run: free, instant and labelled."""
    previous = result.cost_comparison
    comparison = CostComparison(
        baseline_name=previous.baseline_name if previous else "baseline",
        baseline_model_id=previous.baseline_model_id if previous else None,
        fusion_total_cost_usd=0.0,
        baseline_estimated_cost_usd=None,
        savings_usd=None,
        savings_percent=None,
        fusion_is_cheaper=None,
        fusion_cost_known=True,
        baseline_cost_known=False,
        comparison_notes=["Served from the response cache; no model was called."],
    )
    return dataclasses.replace(
        result,
        total_cost_usd=0.0,
        total_latency_ms=lookup_ms,
        ledger=RunLedger(),
        usage=UsageSummary(fusion_wall_latency_ms=round(lookup_ms)),
        cost_comparison=comparison,
        fanout=None,
        refinement=None,
        shadow=None,
        warnings=[
            *result.warnings,
            f"Served from the response cache (answered {age_s:.0f}s ago); no model was called",
        ],
        cache_hit=True,
    )


class ResponseCache:
    """Least-recently-used answers with a time to live."""

    def __init__(
        self, config: CacheConfig, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._config = config
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, PipelineResult]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def get(self, key: str) -> PipelineResult | None:
        began = self._clock()
        entry = self._entries.get(key)
        if entry is None or began - entry[0] > self._config.ttl_seconds:
            self._entries.pop(key, None)
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        stored_at, result = entry
        return _cached_view(
            result, age_s=began - stored_at, lookup_ms=(self._clock() - began) * 1000
        )

    def put(self, key: str, result: PipelineResult) -> None:
        self._entries[key] = (self._clock(), result)
        self._entries.move_to_end(key)
        while len(self._entries) > self._config.max_entries:
            self._entries.popitem(last=False)
