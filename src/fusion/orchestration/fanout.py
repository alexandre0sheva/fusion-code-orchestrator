"""Concurrent fan-out to panel models with timeout and quorum controls."""

from __future__ import annotations

import asyncio
import time
from typing import Literal

from pydantic import BaseModel, Field

from fusion.config.loader import FanoutConfig, ModelEntry
from fusion.orchestration.ledger import CallGateway, call_status, standalone_gateway
from fusion.orchestration.prompts import build_user_prompt, get_role_prompt, get_system_prompt
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.routing.classifier import TaskType

PanelStatus = Literal["success", "failed", "timeout", "missing_provider", "cancelled"]


class PanelCallResult(BaseModel):
    """Structured outcome for a single panel model call."""

    model_name: str
    provider: str
    provider_model_id: str
    status: PanelStatus
    response: ModelResponse | None = None
    error: str | None = None
    error_type: str | None = None
    latency_ms: int = 0

    @property
    def success(self) -> bool:
        return self.status == "success" and self.response is not None and self.response.ok


class FanoutResult(BaseModel):
    """Aggregate result of panel fan-out."""

    calls: list[PanelCallResult] = Field(default_factory=list)
    panel_wall_latency_ms: int = 0
    total_model_call_latency_ms: int = 0
    max_model_latency_ms: int = 0
    min_successful_responses: int = 1
    quorum_met: bool = True
    timed_out: bool = False
    warnings: list[str] = Field(default_factory=list)

    @property
    def successful(self) -> list[tuple[str, ModelResponse]]:
        return [
            (call.model_name, call.response)
            for call in self.calls
            if call.success and call.response is not None
        ]

    @property
    def failed_count(self) -> int:
        return len([call for call in self.calls if not call.success])

    @property
    def success_count(self) -> int:
        return len(self.successful)


async def fanout_to_panel(
    *,
    panel_models: list[str],
    registry_models: dict[str, ModelEntry],
    providers: dict[str, ModelProvider],
    task_type: TaskType,
    primary_content: str,
    context: str = "",
    file_snippets: list[str] | None = None,
    changed_files: list[str] | None = None,
    config: FanoutConfig | None = None,
    gateway: CallGateway | None = None,
) -> FanoutResult:
    """Call all panel models concurrently and return structured outcomes.

    Every call is recorded by ``gateway`` (a throwaway one when the caller has no run ledger).
    """
    fanout_config = config or FanoutConfig()
    gateway = gateway or standalone_gateway(registry_models, providers)
    started = time.perf_counter()
    semaphore = asyncio.Semaphore(fanout_config.max_concurrency)
    min_success = min(fanout_config.min_successful_responses, max(len(panel_models), 1))

    user_prompt = build_user_prompt(
        task_type=task_type,
        primary_content=primary_content,
        context=context,
        file_snippets=file_snippets,
        changed_files=changed_files,
    )
    task_prompt = get_system_prompt(task_type)

    async def _call(model_name: str) -> PanelCallResult:
        entry = registry_models[model_name]
        persona = entry.persona
        request = ModelRequest(
            model_id=entry.model_id,
            system_prompt=get_role_prompt(persona) if persona else task_prompt,
            user_prompt=user_prompt,
            max_tokens=entry.max_tokens,
            timeout=fanout_config.per_model_timeout_seconds,
            metadata={"task_type": task_type.value, "role": "panel", "personality": persona},
        )
        async with semaphore:  # the per-model timeout starts once a slot is free
            response = await gateway.call(
                stage="panel",
                alias=model_name,
                request=request,
                timeout=fanout_config.per_model_timeout_seconds,
            )
        return _panel_result(model_name, entry, response, fanout_config)

    tasks = {asyncio.create_task(_call(name)): name for name in panel_models}
    done, pending = await asyncio.wait(tasks, timeout=fanout_config.global_timeout_seconds)
    timed_out = bool(pending)
    if pending and fanout_config.cancel_on_global_timeout:
        for task in pending:
            task.cancel()
        await asyncio.wait(pending, timeout=1.0)

    calls = [_collect(task, name, registry_models) for task, name in tasks.items()]
    return _summarize(calls, started, min_success, timed_out, fanout_config)


def _panel_result(
    model_name: str, entry: ModelEntry, response: ModelResponse, config: FanoutConfig
) -> PanelCallResult:
    status = call_status(response)
    common = {
        "model_name": model_name,
        "provider": entry.provider,
        "provider_model_id": entry.model_id,
        "latency_ms": round(response.latency_ms),
    }
    if status == "success":
        return PanelCallResult(status="success", response=response, **common)  # type: ignore[arg-type]
    if status == "failed":
        return PanelCallResult(
            status="failed",
            response=response,
            error=response.error,
            error_type=response.error_type or "ProviderError",
            **common,  # type: ignore[arg-type]
        )
    messages = {
        "missing_provider": response.error,
        "timeout": f"Timed out after {config.per_model_timeout_seconds:.1f}s",
        "cancelled": "Cancelled by global panel timeout",
    }
    common["latency_ms"] = 0 if status == "missing_provider" else common["latency_ms"]
    return PanelCallResult(
        status=status,
        error=messages[status],
        error_type=response.error_type,
        **common,  # type: ignore[arg-type]
    )


def _collect(
    task: asyncio.Task[PanelCallResult], model_name: str, registry_models: dict[str, ModelEntry]
) -> PanelCallResult:
    """Result of one panel task; a task that never finished is attributed to its own model."""
    if task.done() and not task.cancelled():
        return task.result()
    entry = registry_models.get(model_name)
    cancelled = task.cancelled()
    return PanelCallResult(
        model_name=model_name,
        provider=entry.provider if entry else "<unknown>",
        provider_model_id=entry.model_id if entry else "<unknown>",
        status="cancelled" if cancelled else "timeout",
        error=(
            "Cancelled by global panel timeout"
            if cancelled
            else "Still pending after global panel timeout"
        ),
        error_type="CancelledError" if cancelled else "TimeoutError",
    )


def _summarize(
    calls: list[PanelCallResult],
    started: float,
    min_success: int,
    timed_out: bool,
    config: FanoutConfig,
) -> FanoutResult:
    panel_wall = round((time.perf_counter() - started) * 1000)
    success_count = len([call for call in calls if call.success])
    quorum_met = success_count >= min_success

    warnings: list[str] = []
    if timed_out:
        warnings.append(
            f"Panel global timeout after {config.global_timeout_seconds:.1f}s; "
            "slow calls were cancelled."
        )
    for call in calls:
        if not call.success:
            warnings.append(f"Panel model {call.model_name} {call.status}: {call.error}")
    if not quorum_met:
        warnings.append(
            f"Panel quorum not met: {success_count}/{min_success} successful responses."
        )
    return FanoutResult(
        calls=calls,
        panel_wall_latency_ms=panel_wall,
        total_model_call_latency_ms=sum(call.latency_ms for call in calls),
        max_model_latency_ms=max((call.latency_ms for call in calls), default=0),
        min_successful_responses=min_success,
        quorum_met=quorum_met,
        timed_out=timed_out,
        warnings=warnings,
    )
