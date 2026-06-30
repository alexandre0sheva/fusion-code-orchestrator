"""Disagreement analysis for panel responses."""

from __future__ import annotations

import re
from typing import Any

from fusion.evals.disagreement_eval import compute_disagreement_score, identify_outliers
from fusion.evals.schemas import ModelResponseEval


def _extract_findings(content: str) -> list[str]:
    findings: list[str] = []
    for line in content.splitlines():
        match = re.match(r"^\s*(?:\d+[\.\)]|\*|\-)\s+(.+)", line)
        if match:
            findings.append(match.group(1).strip().lower())
    if not findings and content.strip():
        findings.append(content.strip()[:200].lower())
    return findings


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _similar(a: str, b: str, threshold: float = 0.5) -> bool:
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b) / max(len(words_a), len(words_b))
    return overlap >= threshold


def _group_similar_findings(all_findings: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Group similar findings across models."""
    groups: list[dict[str, Any]] = []
    for model, finding in all_findings:
        placed = False
        for group in groups:
            if _similar(group["representative"], finding):
                group["models"].append(model)
                group["findings"].append(finding)
                placed = True
                break
        if not placed:
            groups.append(
                {
                    "representative": finding,
                    "models": [model],
                    "findings": [finding],
                }
            )
    return groups


def analyze_disagreement(
    evaluations: list[ModelResponseEval],
    *,
    panel_contents: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Analyze consensus, contradictions, and unique insights among panel responses."""
    score = compute_disagreement_score(evaluations)
    outliers = identify_outliers(evaluations)

    all_findings: list[tuple[str, str]] = []
    high_risk: list[str] = []
    unsupported: list[str] = []

    for ev in evaluations:
        if ev.unsupported_claims > 0.5:
            unsupported.append(f"{ev.model_name}: high unsupported claims score")
        if ev.risk_awareness < 0.4:
            high_risk.append(f"{ev.model_name}: low risk awareness")

    contents = panel_contents or []
    for model_name, content in contents:
        for finding in _extract_findings(content):
            all_findings.append((model_name, finding))
            if any(kw in finding for kw in ("high risk", "critical", "exploit", "injection")):
                high_risk.append(f"{model_name}: {finding[:120]}")

    groups = _group_similar_findings(all_findings)
    consensus_items = [
        g["representative"]
        for g in groups
        if len(set(g["models"])) >= max(2, len(evaluations) // 2)
    ]
    unique_insights = [
        g["representative"]
        for g in groups
        if len(g["models"]) == 1
    ]

    contradictions: list[str] = []
    weak_models = {e.model_name for e in evaluations if e.overall_score < 0.4}
    strong_models = {e.model_name for e in evaluations if e.overall_score >= 0.7}
    if weak_models and strong_models:
        contradictions.append(
            f"Score divergence between {', '.join(weak_models)} and {', '.join(strong_models)}"
        )
    for outlier in outliers:
        contradictions.append(f"Outlier model: {outlier}")

    return {
        "disagreement_score": score,
        "consensus": score < 0.3,
        "outlier_models": outliers,
        "consensus_items": consensus_items,
        "contradictions": contradictions,
        "unique_insights": unique_insights,
        "grouped_findings": [
            {"representative": g["representative"], "models": g["models"]} for g in groups
        ],
        "unsupported_claims": unsupported,
        "high_risk_recommendations": high_risk,
    }
