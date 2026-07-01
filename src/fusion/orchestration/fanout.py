"""Concurrent fan-out to panel models with timeout and quorum controls."""

from __future__ import annotations

import asyncio
import time
from typing import Literal

from pydantic import BaseModel, Field

from fusion.config.loader import FanoutConfig, ModelEntry
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
) -> FanoutResult:
    """Call all panel models concurrently and return structured outcomes."""
    fanout_config = config or FanoutConfig()
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
    system_prompt = get_system_prompt(task_type)

    async def _call(model_name: str) -> PanelCallResult:
        entry = registry_models[model_name]
        provider = providers.get(entry.provider)
        if provider is None:
            return PanelCallResult(
                model_name=model_name,
                provider=entry.provider,
                provider_model_id=entry.model_id,
                status="missing_provider",
                error=f"No provider configured for {entry.provider}",
                error_type="MissingProvider",
            )

        personality = _panel_personality(entry)
        role_prompt = get_role_prompt(personality) if personality else system_prompt
        request = ModelRequest(
            model_id=entry.model_id,
            system_prompt=role_prompt,
            user_prompt=user_prompt,
            max_tokens=entry.max_tokens,
            timeout=fanout_config.per_model_timeout_seconds,
            metadata={
                "task_type": task_type.value,
                "role": "panel",
                "personality": personality,
            },
        )
        call_start = time.perf_counter()
        async with semaphore:
            try:
                response = await asyncio.wait_for(
                    provider.safe_complete(request),
                    timeout=fanout_config.per_model_timeout_seconds,
                )
            except TimeoutError:
                latency = round((time.perf_counter() - call_start) * 1000)
                return PanelCallResult(
                    model_name=model_name,
                    provider=entry.provider,
                    provider_model_id=entry.model_id,
                    status="timeout",
                    error=(
                        f"Timed out after {fanout_config.per_model_timeout_seconds:.1f}s"
                    ),
                    error_type="TimeoutError",
                    latency_ms=latency,
                )
            except asyncio.CancelledError:
                latency = round((time.perf_counter() - call_start) * 1000)
                return PanelCallResult(
                    model_name=model_name,
                    provider=entry.provider,
                    provider_model_id=entry.model_id,
                    status="cancelled",
                    error="Cancelled by global panel timeout",
                    error_type="CancelledError",
                    latency_ms=latency,
                )

        response.model_alias = model_name
        latency = round((time.perf_counter() - call_start) * 1000)
        if response.latency_ms <= 0:
            response.latency_ms = float(latency)
        if response.error:
            return PanelCallResult(
                model_name=model_name,
                provider=entry.provider,
                provider_model_id=entry.model_id,
                status="failed",
                response=response,
                error=response.error,
                error_type=response.error_type or "ProviderError",
                latency_ms=round(response.latency_ms),
            )
        return PanelCallResult(
            model_name=model_name,
            provider=entry.provider,
            provider_model_id=entry.model_id,
            status="success",
            response=response,
            latency_ms=round(response.latency_ms),
        )

    tasks = [asyncio.create_task(_call(model_name)) for model_name in panel_models]
    done, pending = await asyncio.wait(tasks, timeout=fanout_config.global_timeout_seconds)

    timed_out = bool(pending)
    if pending and fanout_config.cancel_on_global_timeout:
        for task in pending:
            task.cancel()
        done_after_cancel, _ = await asyncio.wait(pending, timeout=1.0)
        done |= done_after_cancel

    calls: list[PanelCallResult] = []
    for task in tasks:
        if task.done():
            try:
                calls.append(task.result())
            except asyncio.CancelledError:
                # Should be rare because _call catches cancellation, but keep diagnostics.
                calls.append(
                    PanelCallResult(
                        model_name="<unknown>",
                        provider="<unknown>",
                        provider_model_id="<unknown>",
                        status="cancelled",
                        error="Cancelled by global panel timeout",
                        error_type="CancelledError",
                    )
                )
        else:
            calls.append(_pending_call_result(task, registry_models, panel_models, calls))

    panel_wall = round((time.perf_counter() - started) * 1000)
    total_call_latency = sum(call.latency_ms for call in calls)
    max_latency = max((call.latency_ms for call in calls), default=0)
    success_count = len([call for call in calls if call.success])
    quorum_met = success_count >= min_success

    warnings: list[str] = []
    if timed_out:
        warnings.append(
            f"Panel global timeout after {fanout_config.global_timeout_seconds:.1f}s; "
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
        total_model_call_latency_ms=total_call_latency,
        max_model_latency_ms=max_latency,
        min_successful_responses=min_success,
        quorum_met=quorum_met,
        timed_out=timed_out,
        warnings=warnings,
    )


def _pending_call_result(
    task: asyncio.Task[PanelCallResult],
    registry_models: dict[str, ModelEntry],
    panel_models: list[str],
    completed: list[PanelCallResult],
) -> PanelCallResult:
    completed_names = {call.model_name for call in completed}
    pending_names = [name for name in panel_models if name not in completed_names]
    model_name = pending_names[0] if pending_names else "<unknown>"
    entry = registry_models.get(model_name)
    return PanelCallResult(
        model_name=model_name,
        provider=entry.provider if entry else "<unknown>",
        provider_model_id=entry.model_id if entry else "<unknown>",
        status="timeout",
        error="Still pending after global panel timeout",
        error_type="TimeoutError",
    )


def _panel_personality(entry: ModelEntry) -> str | None:
    if "security" in entry.alias:
        return "security_reviewer"
    if entry.quality_tier == "weak":
        return "weak_model"
    if "debug" in entry.strengths:
        return "debugging_hypothesis"
    if "architecture_decision" in entry.strengths:
        return "architecture_advisor"
    if "implementation_plan" in entry.strengths:
        return "implementation_planner"
    return "coding_reviewer"
