"""Agreement between panel answers, in the shape the synthesizer and tool outputs expect.

The numbers come from ``claims.agreement_score`` (claims grouped across models); this module
only phrases them. No keyword matching and no word-overlap heuristics.
"""

from __future__ import annotations

from typing import Any

from fusion.orchestration.claims import AgreementReport, ClaimCluster

_HIGH = {"high", "critical"}


def _where(cluster: ClaimCluster) -> str:
    if not cluster.file:
        return ""
    return f" ({cluster.file}:{cluster.line})" if cluster.line else f" ({cluster.file})"


def _dispute(cluster: ClaimCluster) -> str:
    rated = ", ".join(
        f"{m.model}: {m.claim.severity}" for m in cluster.members if m.claim.severity
    )
    return f"Severity disputed{_where(cluster)}: {cluster.text} [{rated}]"


def _unsupported(clusters: list[ClaimCluster], known_files: list[str] | None) -> list[str]:
    if not known_files:
        return []
    known = {f.lower().lstrip("./") for f in known_files}
    found = []
    for cluster in clusters:
        for member in cluster.members:
            file = member.claim.file
            if file and file.lower().lstrip("./") not in known:
                found.append(f"{member.model}: cites {file}, which is not among the provided files")
    return found


def analyze_disagreement(
    clusters: list[ClaimCluster],
    report: AgreementReport,
    *,
    known_files: list[str] | None = None,
) -> dict[str, Any]:
    """Consensus, contradictions and unique insights among the panel's claims.

    ``disagreement_score`` is ``1 - agreement`` and 0 when fewer than two models answered
    (``low_information`` says so; there is nothing to compare).
    """
    by_id = {c.id: c for c in clusters}
    disagreement = 0.0 if report.low_information else round(1.0 - report.score, 4)
    return {
        "disagreement_score": disagreement,
        "agreement_score": report.score,
        "low_information": report.low_information,
        "consensus": (
            not report.low_information and report.score >= 0.5 and not report.contradicted
        ),
        "outlier_models": report.outliers,
        "consensus_items": [by_id[i].text for i in report.consensus],
        "contradictions": [_dispute(by_id[i]) for i in report.contradicted]
        + [f"Outlier model: {m}" for m in report.outliers],
        "unique_insights": [by_id[i].text for i in report.unique],
        "grouped_findings": [
            {"representative": c.text, "models": c.models, "status": c.status} for c in clusters
        ],
        "unsupported_claims": _unsupported(clusters, known_files),
        "high_risk_recommendations": [
            f"{', '.join(c.models)}: {c.text}" for c in clusters if c.severity in _HIGH
        ],
    }
