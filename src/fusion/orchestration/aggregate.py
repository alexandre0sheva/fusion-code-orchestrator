"""Aggregators that make no model call: ``digest``, ``vote`` and ``best_of``.

Each turns the panel's answers and their claim clusters into the run's final answer using only
what the panel already said, so the aggregation step costs nothing. ``digest`` hands everything to
Claude Code to merge, ``vote`` keeps the points a majority of models backed, and ``best_of``
returns the one answer that agrees most with the others. (``llm`` aggregation, one synthesizer
call, lives in ``synthesize.py``.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fusion.orchestration.claims import (
    AgreementReport,
    ClaimCluster,
    cluster_line,
    top_clusters,
)
from fusion.orchestration.strategy import Strategy
from fusion.providers.base import ModelResponse

__all__ = ["Aggregator", "Picked", "aggregator_for", "build_best_of", "build_digest", "build_vote"]

Aggregator = Literal["solo", "llm", "vote", "best_of", "digest"]


def aggregator_for(strategy: Strategy, *, cascade_exited_early: bool = False) -> Aggregator:
    """Which aggregator produces the final answer for a run of ``strategy``."""
    if strategy.kind == "solo":
        return "solo"
    if strategy.kind == "cascade" and cascade_exited_early:
        assert strategy.cascade is not None
        return strategy.cascade.early_aggregator
    return strategy.aggregator


def _response(model: str, text: str) -> ModelResponse:
    return ModelResponse(provider="fusion", model=model, text=text)


def _by_id(clusters: list[ClaimCluster]) -> dict[str, ClaimCluster]:
    return {c.id: c for c in clusters}


def build_digest(
    panel_responses: list[tuple[str, str]],
    clusters: list[ClaimCluster],
    report: AgreementReport,
) -> ModelResponse:
    """The panel's answers and claim clusters as one document, for Claude Code to aggregate."""
    lines = [
        f"## Panel digest: {len(panel_responses)} answers, no synthesis model was called",
        "",
        "You are the aggregator. Keep the points several models agree on. Check the points where "
        "they differ, or that only one model raised, against the code before relying on them, and "
        "drop what you cannot confirm. Each answer follows the summary of shared points.",
    ]
    if report.low_information:
        lines.append("\nOnly one answer arrived, so there is no agreement to measure.")
    else:
        lines.append(f"\nAgreement {report.score:.2f} across {report.n_models} models.")
    sections = (
        ("Shared by most models", report.consensus),
        ("Severity disputed", report.contradicted),
        ("Raised by one model", report.unique),
    )
    by_id = _by_id(clusters)
    for title, ids in sections:
        if not ids:
            continue
        lines.extend(["", f"### {title}"])
        for cluster in top_clusters((by_id[i] for i in ids), limit=1000):
            lines.append(f"- {cluster_line(cluster)}")
    for model_name, content in panel_responses:
        lines.extend(["", f"### Answer from {model_name}", content.strip()])
    return _response("digest", "\n".join(lines))


def build_vote(
    clusters: list[ClaimCluster], report: AgreementReport, n_answers: int
) -> ModelResponse:
    """Only the points a majority of the models backed, most important first. No model call."""
    by_id = _by_id(clusters)
    kept = top_clusters((by_id[i] for i in report.consensus), limit=1000)
    left_out = len(report.unique) + len(report.contradicted)
    if not kept:
        text = (
            f"## Panel vote: no point was backed by a majority of the {n_answers} models\n\n"
            "Nothing is returned as agreed. Ask again with the `panel-digest` strategy to see "
            "every answer, or use one that synthesizes."
        )
        return _response("vote", text)
    lines = [
        f"## Panel vote: {len(kept)} points backed by a majority of {n_answers} models",
        "",
        f"Agreement {report.score:.2f}.",
        "",
    ]
    lines.extend(f"- {cluster_line(cluster)}" for cluster in kept)
    if left_out:
        lines.extend(
            [
                "",
                f"{left_out} further points were raised by one model or disputed and are left "
                "out; the `panel-digest` strategy lists them.",
            ]
        )
    return _response("vote", "\n".join(lines))


@dataclass(frozen=True)
class Picked:
    """The answer ``best_of`` chose and how much it agrees with the others (0-1)."""

    model: str
    agreement: float


def _answer_agreement(model: str, clusters: list[ClaimCluster], n_models: int) -> float:
    """Mean share of the other models that back each of ``model``'s points (halved if disputed)."""
    mine = [c for c in clusters if model in c.models]
    if not mine or n_models < 2:
        return 0.0
    credit = [
        (0.5 if c.status == "contradicted" else 1.0) * (c.support - 1) / (n_models - 1)
        for c in mine
    ]
    return sum(credit) / len(credit)


def _evidence_rate(model: str, clusters: list[ClaimCluster]) -> float:
    claims = [m.claim for c in clusters for m in c.members if m.model == model]
    return sum(c.has_evidence for c in claims) / len(claims) if claims else 0.0


def pick_best(
    panel_responses: list[tuple[str, str]], clusters: list[ClaimCluster]
) -> Picked:
    """The model whose points the others back most; ties go to better evidence, then to order."""
    n_models = len(panel_responses)
    scored = [
        (
            -_answer_agreement(name, clusters, n_models),
            -_evidence_rate(name, clusters),
            index,
            name,
        )
        for index, (name, _text) in enumerate(panel_responses)
    ]
    best = min(scored)
    return Picked(model=best[3], agreement=-best[0])


def build_best_of(
    panel_responses: list[tuple[str, str]], clusters: list[ClaimCluster]
) -> tuple[ModelResponse, Picked]:
    """The single answer that agrees most with the rest, as it was written. No model call."""
    picked = pick_best(panel_responses, clusters)
    text = next(content for name, content in panel_responses if name == picked.model)
    return _response("best_of", text), picked
