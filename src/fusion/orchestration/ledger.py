"""One cost ledger for every LLM call in a run, and the gateway that fills it.

Every call, whatever its role (panel, refinement, judge, synthesis, shadow baseline, ...), goes
through ``CallGateway.call``, which times it, prices it and appends exactly one ``CallRecord`` to
the run's ``RunLedger``. Cost, token and latency totals are all derived from the ledger, so there
is a single place where accounting happens.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel

from fusion.config.loader import ModelEntry
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.telemetry.cost import ModelUsage, PricingRegistry

StageName = Literal[
    "panel", "refine", "judge", "synthesis", "shadow_baseline", "shadow_judge", "solo", "eval"
]
CallStatus = Literal["success", "failed", "timeout", "cancelled", "missing_provider"]

# Measurement overhead that is never counted as Fusion's own cost.
SHADOW_STAGES: frozenset[str] = frozenset({"shadow_baseline", "shadow_judge"})
# Conservative text-to-token ratio for deciding whether a prompt fits a context window.
CHARS_PER_TOKEN = 3.0
_CONTEXT_SAFETY_TOKENS = 512


class CallRecord(BaseModel):
    """One LLM call, whatever its role."""

    stage: StageName
    model_alias: str
    provider: str
    model_id: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    cost_known: bool = True
    cost_is_estimate: bool = True
    latency_ms: float = 0.0
    # Milliseconds since the run started; makes overlap between calls visible.
    started_at_ms: float = 0.0
    ok: bool = True
    status: CallStatus = "success"
    error: str | None = None
    error_type: str | None = None
    ttft_ms: float | None = None
    output_tokens_per_s: float | None = None
    decode_tokens_per_s: float | None = None
    retries: int = 0
    trimmed: bool = False
    cache_hit: bool = False  # replayed from a response cache: free, with its original latency

    def to_usage(self) -> ModelUsage:
        total = None
        if self.input_tokens is not None or self.output_tokens is not None:
            total = (self.input_tokens or 0) + (self.output_tokens or 0)
        return ModelUsage(
            provider=self.provider,
            model_alias=self.model_alias,
            provider_model_id=self.model_id,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=total,
            cached_input_tokens=self.cached_tokens,
            cache_write_tokens=self.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens,
            estimated_cost_usd=self.cost_usd,
            cost_is_estimate=self.cost_is_estimate,
            cost_known=self.cost_known,
            latency_ms=round(self.latency_ms),
            success=self.ok,
            error_type=self.error_type,
            error=self.error,
        )


class CostTotal(BaseModel):
    usd: float = 0.0
    known: bool = True
    calls: int = 0


class StageTotals(BaseModel):
    calls: int = 0
    ok_calls: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0


class TaskMetrics(BaseModel):
    """What one task cost to complete, measured from the ledger (nothing estimated later)."""

    seconds_to_complete: float
    cost_usd: float
    cost_known: bool
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    calls: int
    retries: int
    # Total output tokens divided by wall-clock seconds: what the caller actually experienced.
    effective_output_tokens_per_s: float | None
    # When the last non-shadow call finished, relative to the start of the run.
    critical_path_ms: float


class RunLedger:
    """Append-only record of every LLM call in one run."""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._t0 = clock()
        self.records: list[CallRecord] = []

    def now_ms(self) -> float:
        return (self._clock() - self._t0) * 1000.0

    def add(self, record: CallRecord) -> None:
        self.records.append(record)

    def _select(
        self, include: set[str] | None, exclude: set[str] | frozenset[str]
    ) -> list[CallRecord]:
        return [
            r
            for r in self.records
            if (include is None or r.stage in include) and r.stage not in exclude
        ]

    def total_cost(
        self,
        *,
        include_stages: set[str] | None = None,
        exclude_stages: set[str] | frozenset[str] = SHADOW_STAGES,
    ) -> CostTotal:
        chosen = self._select(include_stages, exclude_stages)
        return CostTotal(
            usd=sum(r.cost_usd or 0.0 for r in chosen),
            known=all(r.cost_known for r in chosen),
            calls=len(chosen),
        )

    def by_stage(self) -> dict[str, StageTotals]:
        stages: dict[str, StageTotals] = {}
        for r in self.records:
            totals = stages.setdefault(r.stage, StageTotals())
            totals.calls += 1
            totals.ok_calls += 1 if r.ok else 0
            totals.cost_usd += r.cost_usd or 0.0
            totals.cost_known = totals.cost_known and r.cost_known
            totals.input_tokens += r.input_tokens or 0
            totals.output_tokens += r.output_tokens or 0
            totals.latency_ms += r.latency_ms
        return stages

    def ordered_records(
        self, model_order: list[str] | None = None, *, include_shadow: bool = False
    ) -> list[CallRecord]:
        """Records in stage order, then panel order: stable however the calls interleaved."""
        rank = {name: i for i, name in enumerate(model_order or [])}
        stage_rank = {
            "panel": 0,
            "solo": 0,
            "refine": 1,
            "judge": 2,
            "eval": 3,
            "synthesis": 4,
            "shadow_baseline": 5,
            "shadow_judge": 6,
        }
        chosen = self.records if include_shadow else self._select(None, SHADOW_STAGES)
        return sorted(chosen, key=lambda r: (stage_rank[r.stage], rank.get(r.model_alias, 999)))

    def usage_models(self, model_order: list[str] | None = None) -> list[ModelUsage]:
        """Per-call usage for everything that counts as Fusion's own work."""
        return [r.to_usage() for r in self.ordered_records(model_order)]

    def task_metrics(self, wall_ms: float) -> TaskMetrics:
        chosen = self._select(None, SHADOW_STAGES)
        cost = self.total_cost()
        output_tokens = sum(r.output_tokens or 0 for r in chosen)
        seconds = wall_ms / 1000.0
        return TaskMetrics(
            seconds_to_complete=seconds,
            cost_usd=cost.usd,
            cost_known=cost.known,
            input_tokens=sum(r.input_tokens or 0 for r in chosen),
            output_tokens=output_tokens,
            reasoning_tokens=sum(r.reasoning_tokens or 0 for r in chosen),
            calls=len(chosen),
            retries=sum(r.retries for r in chosen),
            effective_output_tokens_per_s=output_tokens / seconds if seconds > 0 else None,
            critical_path_ms=max((r.started_at_ms + r.latency_ms for r in chosen), default=0.0),
        )

    def timeline(self) -> list[dict[str, object]]:
        """Every call as a (start, end) span in milliseconds since the run began, earliest first."""
        spans = [
            {
                "stage": r.stage,
                "model": r.model_alias,
                "start_ms": r.started_at_ms,
                "end_ms": r.started_at_ms + r.latency_ms,
                "status": r.status,
            }
            for r in self.records
        ]
        return sorted(spans, key=lambda s: (float(str(s["start_ms"])), str(s["model"])))

    def summary(self) -> dict[str, object]:
        """JSON-friendly view stored with the run."""
        return {
            "calls": [r.model_dump() for r in self.records],
            "by_stage": {k: v.model_dump() for k, v in self.by_stage().items()},
        }


def fit_to_context(
    request: ModelRequest, entry: ModelEntry | None
) -> tuple[ModelRequest, str | None]:
    """Trim an oversized user prompt (middle first) to the model's context window.

    Returns the request to send and a human-readable note when something was cut. Models with no
    declared context window are never trimmed.
    """
    window = entry.context_window if entry else None
    if not window or not request.user_prompt:
        return request, None
    other = len(request.system_prompt) + sum(len(m.content) for m in request.messages)
    reserved_output = min(request.max_tokens, window // 2)
    budget_tokens = window - reserved_output - _CONTEXT_SAFETY_TOKENS
    allowed = int(budget_tokens * CHARS_PER_TOKEN) - other
    prompt = request.user_prompt
    if allowed <= 0 or len(prompt) <= allowed:
        return request, None
    marker = (
        f"\n\n[... {len(prompt) - allowed:,} characters omitted to fit the context window ...]\n\n"
    )
    keep = max(allowed - len(marker), 0)
    head = int(keep * 0.6)
    trimmed = prompt[:head] + marker + prompt[len(prompt) - (keep - head) :]
    note = (
        f"Input for {entry.alias if entry else 'model'} was trimmed from {len(prompt):,} to "
        f"{len(trimmed):,} characters to fit its {window:,}-token context window"
    )
    return request.model_copy(update={"user_prompt": trimmed}), note


class CallGateway:
    """The only way a pipeline talks to a model: times, prices and records each call."""

    def __init__(
        self,
        *,
        ledger: RunLedger,
        models: dict[str, ModelEntry],
        providers: dict[str, ModelProvider],
        pricing: PricingRegistry,
        warnings: list[str] | None = None,
        truncate_prompts: bool = True,
        temperature: float | None = None,
        seed: int | None = None,
        stream: bool = False,
    ) -> None:
        self.ledger = ledger
        self.models = models
        self.providers = providers
        self.pricing = pricing
        self.warnings = warnings if warnings is not None else []
        # Run-wide defaults (benchmark mode fixes them); a request that sets its own value wins.
        self.truncate_prompts = truncate_prompts
        self.temperature = temperature
        self.seed = seed
        self.stream = stream

    async def call(
        self,
        *,
        stage: StageName,
        alias: str,
        request: ModelRequest,
        timeout: float | None = None,
        provider_name: str | None = None,
    ) -> ModelResponse:
        """Run one call. Failures come back as responses with ``error`` set, never as raises."""
        entry = self.models.get(alias)
        provider_key = provider_name or (entry.provider if entry else "")
        provider = self.providers.get(provider_key)
        started_ms = self.ledger.now_ms()
        if provider is None:
            response = ModelResponse(
                provider=provider_key,
                model=request.model_id,
                error=f"No provider configured for {provider_key}",
                error_type="MissingProvider",
            )
            return self._record(stage, alias, response, entry, started_ms, "missing_provider")

        request = self._with_sampling(request, entry)
        note = None
        if self.truncate_prompts:
            request, note = fit_to_context(request, entry)
        if note:
            self.warnings.append(note)
        began = time.perf_counter()
        status: CallStatus = "success"
        try:
            coro = provider.safe_complete(request)
            response = await (asyncio.wait_for(coro, timeout) if timeout else coro)
        except TimeoutError:
            status = "timeout"
            response = self._failure(
                provider, request, "TimeoutError", f"Timed out after {timeout:.1f}s"
            )
        except asyncio.CancelledError:
            status = "cancelled"
            response = self._failure(provider, request, "CancelledError", "Cancelled")
        measured = (time.perf_counter() - began) * 1000.0
        if response.latency_ms <= 0:
            response.latency_ms = measured
        if status == "success" and not response.ok:
            status = "failed"
        return self._record(stage, alias, response, entry, started_ms, status, trimmed=bool(note))

    def _with_sampling(self, request: ModelRequest, entry: ModelEntry | None) -> ModelRequest:
        update: dict[str, object] = {}
        if request.temperature is None and self.temperature is not None:
            update["temperature"] = self.temperature
        if request.seed is None and self.seed is not None:
            update["seed"] = self.seed
        if self.stream and not request.stream and (entry is None or entry.supports_streaming):
            update["stream"] = True
        return request.model_copy(update=update) if update else request

    @staticmethod
    def _failure(
        provider: ModelProvider, request: ModelRequest, error_type: str, message: str
    ) -> ModelResponse:
        return ModelResponse(
            provider=provider.name, model=request.model_id, error=message, error_type=error_type
        )

    def _record(
        self,
        stage: StageName,
        alias: str,
        response: ModelResponse,
        entry: ModelEntry | None,
        started_ms: float,
        status: CallStatus,
        *,
        trimmed: bool = False,
    ) -> ModelResponse:
        response.model_alias = alias
        if status == "missing_provider" or response.cache_hit:
            cost_usd: float | None = 0.0  # nothing was sent, so nothing was billed
            cost_known, cost_is_estimate = True, False
        else:
            cost = self.pricing.estimate_response_cost(response, entry)
            cost_usd, cost_known, cost_is_estimate = cost.amount_usd, cost.known, cost.is_estimate
        self.ledger.add(
            CallRecord(
                stage=stage,
                model_alias=alias,
                provider=response.provider,
                model_id=response.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cached_tokens=response.cached_input_tokens,
                cache_write_tokens=response.cache_write_tokens,
                reasoning_tokens=response.reasoning_tokens,
                cost_usd=cost_usd,
                cost_known=cost_known,
                cost_is_estimate=cost_is_estimate,
                latency_ms=response.latency_ms,
                started_at_ms=started_ms,
                ok=response.ok,
                status=status,
                error=response.error,
                error_type=response.error_type,
                ttft_ms=response.ttft_ms,
                output_tokens_per_s=response.total_tokens_per_s,
                decode_tokens_per_s=response.decode_tokens_per_s,
                retries=response.retries,
                trimmed=trimmed,
                cache_hit=response.cache_hit,
            )
        )
        return response


def standalone_gateway(
    models: dict[str, ModelEntry],
    providers: dict[str, ModelProvider],
    *,
    pricing: PricingRegistry | None = None,
) -> CallGateway:
    """A gateway with its own throwaway ledger, for callers that run a step outside a pipeline."""
    return CallGateway(
        ledger=RunLedger(), models=models, providers=providers, pricing=pricing or PricingRegistry()
    )


def call_status(response: ModelResponse) -> CallStatus:
    """Classify a gateway response (the gateway reports failures through ``error_type``)."""
    if response.error_type == "MissingProvider":
        return "missing_provider"
    if response.error_type == "TimeoutError":
        return "timeout"
    if response.error_type == "CancelledError":
        return "cancelled"
    return "failed" if response.error else "success"
