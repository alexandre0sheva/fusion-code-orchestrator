"""Synthesize panel responses into a final recommendation."""

from __future__ import annotations

from fusion.config.loader import ModelEntry
from fusion.orchestration.prompts import build_synthesis_prompt, get_role_prompt
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse, ProviderError
from fusion.routing.classifier import TaskType


async def synthesize_responses(
    *,
    synthesizer_model: str,
    registry_models: dict[str, ModelEntry],
    providers: dict[str, ModelProvider],
    task_type: TaskType,
    panel_responses: list[tuple[str, str]],
    disagreement_analysis: dict[str, object],
    original_task: str = "",
) -> ModelResponse:
    """Call synthesizer model to merge panel responses into structured JSON."""
    entry = registry_models[synthesizer_model]
    provider = providers.get(entry.provider)
    if provider is None:
        return ModelResponse(
            provider=entry.provider,
            model=entry.model_id,
            error=f"No provider for {entry.provider}",
        )

    user_prompt = build_synthesis_prompt(
        task_type=task_type,
        panel_responses=panel_responses,
        disagreement_analysis=disagreement_analysis,
        original_task=original_task,
    )
    request = ModelRequest(
        model_id=entry.model_id,
        system_prompt=get_role_prompt("synthesizer"),
        user_prompt=user_prompt,
        max_tokens=entry.max_tokens,
        json_mode=entry.supports_json,
        metadata={
            "task_type": task_type.value,
            "role": "synthesizer",
            "personality": "synthesizer",
        },
    )
    response = await provider.safe_complete(request)
    if response.error:
        raise ProviderError(response.error)
    return response
