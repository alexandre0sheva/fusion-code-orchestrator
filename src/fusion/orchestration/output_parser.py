"""Turn the final answer into the task-specific fields tool outputs expose.

A synthesizer's JSON maps straight onto the fields. Without it (a solo run, a digest, or a
synthesis that was not JSON) the fields are built from the panel's claim clusters.
"""

from __future__ import annotations

import json
from typing import Any

from fusion.orchestration.claims import ClaimCluster, top_clusters
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


def parse_structured_output(
    task_type: TaskType,
    content: str,
    *,
    disagreement: dict[str, Any] | None = None,
    confidence: float = 0.5,
    clusters: list[ClaimCluster] | None = None,
    summary: str = "",
    score: float | None = None,
) -> dict[str, Any]:
    """Task-specific fields from synthesizer JSON, else from the claim clusters.

    ``confidence`` is the calibrated value and always wins over a model's own estimate.
    """
    parsed = _extract_json(content)
    key = canonical_task_key(task_type)
    disagreement = disagreement or {}
    if parsed:
        result = _from_json(key, parsed, disagreement)
    else:
        result = _from_claims(key, clusters or [], disagreement, summary or content, score)
    result["confidence"] = confidence
    return result


def _from_json(
    key: str,
    data: dict[str, Any],
    disagreement: dict[str, Any],
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
    patch = data.get("patch")
    return {
        "answer": answer,
        **({"patch": patch} if isinstance(patch, str) and patch.strip() else {}),
        "summary": str(data.get("summary", answer[:500])),
        "suggested_actions": _as_str_list(data.get("suggested_actions")),
        "tests_to_run": _as_str_list(data.get("tests_to_run")),
        "risks": _as_str_list(data.get("risks")),
        "assumptions": _as_str_list(data.get("assumptions")),
        "confidence": float(data.get("confidence", 0.5)),
    }


def _texts(clusters: list[ClaimCluster], kind: str) -> list[str]:
    return [c.text for c in top_clusters((c for c in clusters if c.kind == kind), limit=1000)]


def _from_claims(
    key: str,
    clusters: list[ClaimCluster],
    disagreement: dict[str, Any],
    summary: str,
    score: float | None,
) -> dict[str, Any]:
    """Fill a task's fields from claims of the matching kind, most important first."""
    findings, recs = _texts(clusters, "finding"), _texts(clusters, "recommendation")
    tests, risks = _texts(clusters, "test"), _texts(clusters, "risk")
    hypotheses = top_clusters((c for c in clusters if c.kind == "hypothesis"), limit=1000)
    n = max((len(c.models) for c in clusters), default=1)
    if key == "code_review":
        return {
            "summary": summary,
            "critical_findings": findings,
            "recommended_changes": recs,
            "false_positive_risks": [],
            "test_plan": tests,
            "consensus": _as_str_list(disagreement.get("consensus_items")),
            "disagreements": _as_str_list(disagreement.get("contradictions")),
            "unique_insights": _as_str_list(disagreement.get("unique_insights")),
        }
    if key == "debugging":
        return {
            "most_likely_causes": [c.text for c in hypotheses[:3]],
            "ranked_hypotheses": [
                {
                    "hypothesis": c.text,
                    "confidence": round(c.support / n, 2),
                    "evidence": next((m.claim.evidence for m in c.members if m.claim.evidence), ""),
                }
                for c in hypotheses
            ],
            "verification_steps": tests,
            "minimal_fix_strategy": recs[0] if recs else "",
            "what_not_to_do": [],
        }
    if key == "architecture_decision":
        return {
            "recommended_option": recs[0] if recs else summary,
            "tradeoffs": findings,
            "rejected_options": [],
            "risks": risks,
            "reversibility": "",
            "migration_plan": recs[1:],
            "test_strategy": tests,
        }
    if key == "implementation_plan":
        return {
            "implementation_sequence": recs,
            "affected_modules": [],
            "data_model_changes": [],
            "api_changes": [],
            "ui_changes": [],
            "tests_to_add": tests,
            "risks": risks,
            "open_questions": [],
        }
    if key == "answer_eval":
        return {
            "score": score if score is not None else 0.0,
            "strengths": [],
            "weaknesses": findings,
            "unsupported_claims": _as_str_list(disagreement.get("unsupported_claims")),
            "missing_points": recs,
            "safer_answer": "",
        }
    return {
        "answer": summary,
        "summary": summary,
        "suggested_actions": recs,
        "tests_to_run": tests,
        "risks": risks,
        "assumptions": [c.text for c in hypotheses],
    }
