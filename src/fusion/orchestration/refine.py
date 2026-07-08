"""Mixture-of-agents refinement round.

After the first panel fan-out, each surviving model sees the other models'
anonymized answers and revises its own. A model whose refinement call fails
keeps its round-1 answer, so refinement can degrade to a no-op but never
lose information.
"""

from __future__ import annotations

import asyncio
import string
import time

from pydantic import BaseModel, Field

from fusion.config.loader import ModelEntry, RefinementConfig
from fusion.orchestration.prompts import build_refinement_prompt, get_system_prompt
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.routing.classifier import TaskType


class RefineCallResult(BaseModel):
    """Structured outcome for a single refinement call."""

    model_name: str
    provider: str
    provider_model_id: str
    refined: bool = False
    response: ModelResponse | None = None
    error: str | None = None
    error_type: str | None = None
    latency_ms: int = 0


class RefinementResult(BaseModel):
    """Aggregate result of the refinement round."""

    ran: bool = False
    calls: list[RefineCallResult] = Field(default_factory=list)
    refinement_wall_latency_ms: int = 0
    warnings: list[str] = Field(default_factory=list)

    @property
    def refined_count(self) -> int:
        return len([call for call in self.calls if call.refined])


async def refine_panel_responses(
    *,
    responses: list[tuple[str, ModelResponse]],
    registry_models: dict[str, ModelEntry],
    providers: dict[str, ModelProvider],
    task_type: TaskType,
    original_task: str,
    config: RefinementConfig | None = None,
) -> tuple[list[tuple[str, ModelResponse]], RefinementResult]:
    """Run one anonymized peer-review round over successful panel responses.

    Returns the responses in original order — refined where the second call
    succeeded, round-1 otherwise — plus refinement telemetry.
    """
    refine_config = config or RefinementConfig()
    result = RefinementResult()
    if len(responses) < refine_config.min_panel_size:
        result.warnings.append(
            f"Refinement skipped: {len(responses)} panel responses below "
            f"minimum of {refine_config.min_panel_size}"
        )
        return responses, result

    started = time.perf_counter()
    result.ran = True
    labels = string.ascii_uppercase
    system_prompt = get_system_prompt(task_type)

    async def _refine(index: int, model_name: str, own: ModelResponse) -> RefineCallResult:
        entry = registry_models[model_name]
        provider = providers.get(entry.provider)
        if provider is None:
            return RefineCallResult(
                model_name=model_name,
                provider=entry.provider,
                provider_model_id=entry.model_id,
                error=f"No provider configured for {entry.provider}",
                error_type="MissingProvider",
            )
        peers = [
            (labels[i % len(labels)], peer_response.content)
            for i, (_, peer_response) in enumerate(responses)
            if i != index
        ]
        request = ModelRequest(
            model_id=entry.model_id,
            system_prompt=system_prompt,
            user_prompt=build_refinement_prompt(
                task_type=task_type,
                original_task=original_task,
                own_answer=own.content,
                peer_answers=peers,
            ),
            max_tokens=entry.max_tokens,
            timeout=refine_config.per_model_timeout_seconds,
            metadata={"task_type": task_type.value, "role": "refine"},
        )
        call_start = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                provider.safe_complete(request),
                timeout=refine_config.per_model_timeout_seconds,
            )
        except TimeoutError:
            return RefineCallResult(
                model_name=model_name,
                provider=entry.provider,
                provider_model_id=entry.model_id,
                error=f"Timed out after {refine_config.per_model_timeout_seconds:.1f}s",
                error_type="TimeoutError",
                latency_ms=round((time.perf_counter() - call_start) * 1000),
            )
        except asyncio.CancelledError:
            return RefineCallResult(
                model_name=model_name,
                provider=entry.provider,
                provider_model_id=entry.model_id,
                error="Cancelled by global refinement timeout",
                error_type="CancelledError",
                latency_ms=round((time.perf_counter() - call_start) * 1000),
            )

        response.model_alias = model_name
        latency = round((time.perf_counter() - call_start) * 1000)
        if response.latency_ms <= 0:
            response.latency_ms = float(latency)
        if response.error or not response.content.strip():
            return RefineCallResult(
                model_name=model_name,
                provider=entry.provider,
                provider_model_id=entry.model_id,
                response=response,
                error=response.error or "Empty refinement response",
                error_type=response.error_type or "EmptyResponse",
                latency_ms=round(response.latency_ms),
            )
        return RefineCallResult(
            model_name=model_name,
            provider=entry.provider,
            provider_model_id=entry.model_id,
            refined=True,
            response=response,
            latency_ms=round(response.latency_ms),
        )

    tasks = [
        asyncio.create_task(_refine(i, name, response))
        for i, (name, response) in enumerate(responses)
    ]
    done, pending = await asyncio.wait(tasks, timeout=refine_config.global_timeout_seconds)
    if pending:
        for task in pending:
            task.cancel()
        done_after_cancel, _ = await asyncio.wait(pending, timeout=1.0)
        done |= done_after_cancel
        result.warnings.append(
            f"Refinement global timeout after {refine_config.global_timeout_seconds:.1f}s; "
            "slow calls kept their round-1 answers."
        )

    calls_by_model: dict[str, RefineCallResult] = {}
    for i, task in enumerate(tasks):
        model_name = responses[i][0]
        call: RefineCallResult
        if task.done() and not task.cancelled():
            call = task.result()
        else:
            entry = registry_models[model_name]
            call = RefineCallResult(
                model_name=model_name,
                provider=entry.provider,
                provider_model_id=entry.model_id,
                error="Still pending after global refinement timeout",
                error_type="TimeoutError",
            )
        calls_by_model[model_name] = call
        result.calls.append(call)

    refined_responses: list[tuple[str, ModelResponse]] = []
    for model_name, original in responses:
        outcome = calls_by_model.get(model_name)
        if outcome is not None and outcome.refined and outcome.response is not None:
            refined_responses.append((model_name, outcome.response))
        else:
            refined_responses.append((model_name, original))
            if outcome is not None and outcome.error:
                result.warnings.append(
                    f"Refinement for {model_name} failed ({outcome.error}); "
                    "kept round-1 answer"
                )

    result.refinement_wall_latency_ms = round((time.perf_counter() - started) * 1000)
    return refined_responses, result
