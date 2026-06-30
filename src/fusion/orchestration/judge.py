"""Judge orchestration helpers."""

from __future__ import annotations

from fusion.evals.engine import EvalEngine
from fusion.evals.schemas import ModelResponseEval
from fusion.providers.base import ModelResponse


async def judge_panel_responses(
    *,
    eval_engine: EvalEngine,
    responses: list[tuple[str, ModelResponse]],
    task_type: str,
    judge_model: str,
    context: str = "",
    is_coding_task: bool = False,
    known_files: list[str] | None = None,
) -> list[ModelResponseEval]:
    """Evaluate each panel response."""
    evaluations: list[ModelResponseEval] = []
    for model_name, response in responses:
        ev = await eval_engine.evaluate_response(
            model_name=model_name,
            content=response.content,
            task_type=task_type,
            judge_model=judge_model,
            context=context,
            is_coding_task=is_coding_task,
            known_files=known_files,
        )
        evaluations.append(ev)
    return evaluations
