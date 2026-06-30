"""Concurrent fan-out to panel models."""

from __future__ import annotations

from fusion.config.loader import ModelEntry
from fusion.orchestration.prompts import build_user_prompt, get_role_prompt, get_system_prompt
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.routing.classifier import TaskType


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
) -> list[tuple[str, ModelResponse]]:
    """Call all panel models concurrently."""
    import asyncio

    user_prompt = build_user_prompt(
        task_type=task_type,
        primary_content=primary_content,
        context=context,
        file_snippets=file_snippets,
        changed_files=changed_files,
    )
    system_prompt = get_system_prompt(task_type)

    async def _call(model_name: str) -> tuple[str, ModelResponse]:
        entry = registry_models[model_name]
        provider = providers.get(entry.provider)
        if provider is None:
            return model_name, ModelResponse(
                provider=entry.provider,
                model=entry.model_id,
                error=f"No provider for {entry.provider}",
            )
        personality = _panel_personality(entry)
        role_prompt = get_role_prompt(personality) if personality else system_prompt
        request = ModelRequest(
            model_id=entry.model_id,
            system_prompt=role_prompt,
            user_prompt=user_prompt,
            max_tokens=entry.max_tokens,
            metadata={
                "task_type": task_type.value,
                "role": "panel",
                "personality": personality,
            },
        )
        return model_name, await provider.safe_complete(request)

    results = await asyncio.gather(*[_call(m) for m in panel_models])
    return list(results)


def _panel_personality(entry: ModelEntry) -> str | None:
    if "security" in entry.alias:
        return "security_reviewer"
    if entry.quality_tier == "weak":
        return "weak_model"
    if "debug" in entry.strengths:
        return "debugging_hypothesis"
    if "architecture_decision" in entry.strengths:
        return "architecture_advisor"
    return "coding_reviewer"
