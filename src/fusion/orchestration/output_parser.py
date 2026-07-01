"""Parse synthesizer output into task-specific structured fields."""

from __future__ import annotations

import json
import re
from typing import Any

from fusion.routing.classifier import TaskType, canonical_task_key


def _extract_json(content: str) -> dict[str, Any] | None:
    try:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(content[start:end])
            if isinstance(parsed, dict):
                return parsed
    except json.JSONDecodeError:
        pass
    return None


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)]


def _extract_numbered_items(content: str) -> list[str]:
    items: list[str] = []
    for line in content.splitlines():
        match = re.match(r"^\s*(?:\d+[\.\)]|\*|\-)\s+(.+)", line)
        if match:
            items.append(match.group(1).strip())
    return items


def _extract_section(content: str, header: str) -> str:
    pattern = rf"(?i)##?\s*{re.escape(header)}[^\n]*\n(.*?)(?=\n##|\Z)"
    match = re.search(pattern, content, re.DOTALL)
    return match.group(1).strip() if match else ""


def parse_structured_output(
    task_type: TaskType,
    content: str,
    *,
    disagreement: dict[str, Any] | None = None,
    confidence: float = 0.5,
) -> dict[str, Any]:
    """Parse synthesis content into task-specific structured fields."""
    parsed = _extract_json(content)
    key = canonical_task_key(task_type)
    disagreement = disagreement or {}

    if parsed:
        return _from_json(key, parsed, disagreement, confidence)

    return _from_markdown(key, content, disagreement, confidence)


def _from_json(
    key: str,
    data: dict[str, Any],
    disagreement: dict[str, Any],
    confidence: float,
) -> dict[str, Any]:
    parsers = {
        "code_review": _parse_code_review_json,
        "debugging": _parse_debug_json,
        "architecture_decision": _parse_architecture_json,
        "implementation_plan": _parse_plan_json,
        "answer_eval": _parse_answer_eval_json,
    }
    parser = parsers.get(key, _parse_generic_json)
    result = parser(data)
    if "confidence" not in result or result.get("confidence") is None:
        result["confidence"] = float(data.get("confidence", confidence))
    if key == "code_review":
        result.setdefault("consensus", _as_str_list(disagreement.get("consensus_items")))
        result.setdefault("disagreements", _as_str_list(disagreement.get("contradictions")))
        result.setdefault("unique_insights", _as_str_list(disagreement.get("unique_insights")))
    return result


def _parse_code_review_json(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": str(data.get("summary", "")),
        "critical_findings": _as_str_list(data.get("critical_findings")),
        "recommended_changes": _as_str_list(data.get("recommended_changes")),
        "false_positive_risks": _as_str_list(data.get("false_positive_risks")),
        "test_plan": _as_str_list(data.get("test_plan")),
        "consensus": _as_str_list(data.get("consensus")),
        "disagreements": _as_str_list(data.get("disagreements")),
        "unique_insights": _as_str_list(data.get("unique_insights")),
        "confidence": float(data.get("confidence", 0.6)),
    }


def _parse_debug_json(data: dict[str, Any]) -> dict[str, Any]:
    hypotheses = data.get("ranked_hypotheses", [])
    if isinstance(hypotheses, list):
        ranked = [
            h if isinstance(h, dict) else {"hypothesis": str(h), "confidence": 0.5}
            for h in hypotheses
        ]
    else:
        ranked = []
    return {
        "most_likely_causes": _as_str_list(data.get("most_likely_causes")),
        "ranked_hypotheses": ranked,
        "verification_steps": _as_str_list(data.get("verification_steps")),
        "minimal_fix_strategy": str(data.get("minimal_fix_strategy", "")),
        "what_not_to_do": _as_str_list(data.get("what_not_to_do")),
        "confidence": float(data.get("confidence", 0.6)),
    }


def _parse_architecture_json(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "recommended_option": str(data.get("recommended_option", "")),
        "tradeoffs": _as_str_list(data.get("tradeoffs")),
        "rejected_options": _as_str_list(data.get("rejected_options")),
        "risks": _as_str_list(data.get("risks")),
        "reversibility": str(data.get("reversibility", "")),
        "migration_plan": _as_str_list(data.get("migration_plan")),
        "test_strategy": _as_str_list(data.get("test_strategy")),
        "confidence": float(data.get("confidence", 0.6)),
    }


def _parse_plan_json(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "implementation_sequence": _as_str_list(data.get("implementation_sequence")),
        "affected_modules": _as_str_list(data.get("affected_modules")),
        "data_model_changes": _as_str_list(data.get("data_model_changes")),
        "api_changes": _as_str_list(
            data.get("api_changes") or data.get("API_changes")
        ),
        "ui_changes": _as_str_list(data.get("ui_changes") or data.get("UI_changes")),
        "tests_to_add": _as_str_list(data.get("tests_to_add")),
        "risks": _as_str_list(data.get("risks")),
        "open_questions": _as_str_list(data.get("open_questions")),
        "confidence": float(data.get("confidence", 0.6)),
    }


def _parse_answer_eval_json(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "score": float(data.get("score", data.get("overall_score", 0.5))),
        "strengths": _as_str_list(data.get("strengths")),
        "weaknesses": _as_str_list(data.get("weaknesses")),
        "unsupported_claims": _as_str_list(data.get("unsupported_claims")),
        "missing_points": _as_str_list(data.get("missing_points")),
        "safer_answer": str(data.get("safer_answer", "")),
        "confidence": float(data.get("confidence", 0.6)),
    }


def _parse_generic_json(data: dict[str, Any]) -> dict[str, Any]:
    answer = str(data.get("answer") or data.get("summary") or json.dumps(data)[:500])
    return {
        "answer": answer,
        "summary": str(data.get("summary", answer[:500])),
        "suggested_actions": _as_str_list(data.get("suggested_actions")),
        "tests_to_run": _as_str_list(data.get("tests_to_run")),
        "risks": _as_str_list(data.get("risks")),
        "assumptions": _as_str_list(data.get("assumptions")),
        "confidence": float(data.get("confidence", 0.5)),
    }


def _from_markdown(
    key: str,
    content: str,
    disagreement: dict[str, Any],
    confidence: float,
) -> dict[str, Any]:
    items = _extract_numbered_items(content)
    summary = _extract_section(content, "Final Recommendation") or content[:500]

    if key == "code_review":
        return {
            "summary": summary,
            "critical_findings": items[:3] or ["See synthesis for details"],
            "recommended_changes": items[3:6] if len(items) > 3 else items,
            "false_positive_risks": [],
            "test_plan": [i for i in items if "test" in i.lower()],
            "consensus": _as_str_list(disagreement.get("consensus_items")),
            "disagreements": _as_str_list(disagreement.get("contradictions")),
            "unique_insights": _as_str_list(disagreement.get("unique_insights")),
            "confidence": confidence,
        }
    if key == "debugging":
        return {
            "most_likely_causes": items[:2] or ["See synthesis"],
            "ranked_hypotheses": [{"hypothesis": i, "confidence": 0.5} for i in items],
            "verification_steps": items,
            "minimal_fix_strategy": summary,
            "what_not_to_do": [],
            "confidence": confidence,
        }
    if key == "architecture_decision":
        return {
            "recommended_option": summary.split("\n")[0] if summary else "",
            "tradeoffs": items,
            "rejected_options": [],
            "risks": [i for i in items if "risk" in i.lower()],
            "reversibility": "",
            "migration_plan": items,
            "test_strategy": [i for i in items if "test" in i.lower()],
            "confidence": confidence,
        }
    if key == "implementation_plan":
        return {
            "implementation_sequence": items,
            "affected_modules": [],
            "data_model_changes": [],
            "api_changes": [],
            "ui_changes": [],
            "tests_to_add": [i for i in items if "test" in i.lower()],
            "risks": [],
            "open_questions": [],
            "confidence": confidence,
        }
    if key == "answer_eval":
        return {
            "score": confidence,
            "strengths": items[:2],
            "weaknesses": items[2:4] if len(items) > 2 else [],
            "unsupported_claims": [],
            "missing_points": [],
            "safer_answer": summary,
            "confidence": confidence,
        }
    return {
        "answer": content,
        "summary": summary,
        "suggested_actions": items,
        "tests_to_run": [i for i in items if "test" in i.lower()],
        "risks": [i for i in items if "risk" in i.lower()],
        "assumptions": [],
        "confidence": confidence,
    }
