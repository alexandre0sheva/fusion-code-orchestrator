"""LLM-as-a-judge evaluation."""

from __future__ import annotations

import json
import re
from typing import Any

from fusion.providers.base import ModelProvider, ModelRequest
from fusion.routing.model_registry import ModelRegistry


async def judge_response(
    *,
    provider: ModelProvider,
    registry: ModelRegistry,
    judge_model: str,
    response_content: str,
    task_type: str,
    context: str = "",
) -> dict[str, Any]:
    """Use an LLM judge to score a model response."""
    model_entry = registry.get(judge_model)
    prompt = (
        f"Evaluate this {task_type} response on a 0-1 scale for each dimension.\n"
        f"Return JSON with keys: specificity, groundedness, actionability, "
        f"correctness_likelihood, risk_awareness, unsupported_claims, "
        f"codebase_awareness, novelty, overall_score, notes.\n\n"
        f"Context:\n{context[:2000]}\n\nResponse:\n{response_content[:4000]}"
    )
    request = ModelRequest(
        model_id=model_entry.model_id,
        system_prompt="You are an evaluation judge. Return only valid JSON.",
        user_prompt=prompt,
        max_tokens=1024,
        json_mode=model_entry.supports_json,
        metadata={"role": "judge", "personality": "judge", "task_type": task_type},
    )
    completion = await provider.safe_complete(request)
    if completion.error:
        return heuristic_judge_scores(response_content, notes=completion.error)
    if completion.parsed_json:
        return _merge_judge_defaults(completion.parsed_json)
    return _parse_judge_scores(completion.text)


def _merge_judge_defaults(parsed: dict[str, Any]) -> dict[str, Any]:
    defaults = _default_judge_scores()
    defaults.update(parsed)
    return defaults


def _parse_judge_scores(content: str) -> dict[str, Any]:
    """Parse judge JSON from response content."""
    defaults = _default_judge_scores()
    try:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(content[start:end])
            defaults.update(parsed)
    except (json.JSONDecodeError, ValueError):
        defaults["notes"] = "Failed to parse judge JSON; using defaults"
    return defaults


def _default_judge_scores() -> dict[str, Any]:
    return {
        "specificity": 0.5,
        "groundedness": 0.5,
        "actionability": 0.5,
        "correctness_likelihood": 0.5,
        "risk_awareness": 0.5,
        "unsupported_claims": 0.3,
        "codebase_awareness": 0.5,
        "novelty": 0.5,
        "overall_score": 0.5,
        "notes": "",
    }


def heuristic_judge_scores(content: str, notes: str = "Heuristic scoring") -> dict[str, Any]:
    """Fallback heuristic scoring when no LLM judge is available."""
    length_score = min(1.0, len(content) / 500.0)
    has_structure = 1.0 if "##" in content or "**" in content else 0.5
    has_action = 1.0 if re.search(r"\d+\.", content) else 0.4
    base = (length_score + has_structure + has_action) / 3.0
    return {
        "specificity": base,
        "groundedness": base * 0.9,
        "actionability": has_action,
        "correctness_likelihood": base * 0.8,
        "risk_awareness": 0.6 if "risk" in content.lower() else 0.4,
        "unsupported_claims": 0.2,
        "codebase_awareness": 0.5,
        "novelty": 0.4,
        "overall_score": base,
        "notes": notes,
    }
