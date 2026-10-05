"""Judge orchestration helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fusion.evals.engine import EvalEngine
from fusion.evals.schemas import ModelResponseEval
from fusion.providers.base import ModelResponse

if TYPE_CHECKING:
    from fusion.orchestration.ledger import CallGateway


async def judge_panel_responses(
    *,
    eval_engine: EvalEngine,
    responses: list[tuple[str, ModelResponse]],
    task_type: str,
    judge_model: str,
    context: str = "",
    is_coding_task: bool = False,
    known_files: list[str] | None = None,
    gateway: CallGateway | None = None,
    use_llm: bool = True,
) -> list[ModelResponseEval]:
    """Evaluate each panel response (with the LLM judge unless ``use_llm`` is false)."""
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
            gateway=gateway,
            use_llm=use_llm,
        )
        evaluations.append(ev)
    return evaluations
