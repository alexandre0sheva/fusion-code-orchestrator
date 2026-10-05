"""Scoring interface for studies, and the one built-in scorer.

A scorer turns an arm's answer to a task into a quality in [0, 1] using the task's ground truth.
``PointsScorer`` is the deterministic baseline: it checks which of the truth's key points an
answer says and which known-wrong ones it asserts. Other scorers (hidden tests, performance
measurements, LLM judges) implement the same ``Scorer`` protocol and are registered per category
in ``SCORERS``; whatever they spend on judge calls is reported as ``eval_cost_usd``, separately
from what the arm cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from fusion.bench.spec import BenchTask, Category
from fusion.orchestration.ledger import CallGateway

__all__ = [
    "PASS_THRESHOLDS",
    "SCORERS",
    "AnswerView",
    "PointsScorer",
    "ScoreEnv",
    "ScoreResult",
    "Scorer",
    "get_scorer",
    "is_solved",
]

# Quality an answer needs to count as solved, per category.
PASS_THRESHOLDS: dict[Category, float] = {
    "code_review": 0.6,
    "debugging": 0.6,
    "architecture": 0.6,
    "planning": 0.6,
    "coding": 0.6,
    "frontend": 0.6,
    "performance": 0.6,
}


def is_solved(category: Category, quality: float | None) -> bool:
    return quality is not None and quality >= PASS_THRESHOLDS[category]


@dataclass
class AnswerView:
    """What a scorer may look at: the arm's final answer and the claims behind it."""

    final_answer: str
    claims: list[dict[str, Any]] = field(default_factory=list)  # ClaimCluster dumps
    structured: dict[str, Any] = field(default_factory=dict)
    halted: bool = False  # the run stopped early; the answer is a diagnostic, not an attempt

    def text(self) -> str:
        """What the arm answered, for keyword matching. The claims behind it are not added: an
        aggregator that dropped a point must not get credit for a model having raised it."""
        return self.final_answer


@dataclass
class ScoreEnv:
    """Resources a scorer may use. ``gateway`` records its calls in a ledger of its own, so the
    money a scorer spends is kept apart from the arm's."""

    gateway: CallGateway
    judge_models: list[str] = field(default_factory=list)


class ScoreResult(BaseModel):
    quality: float = Field(ge=0.0, le=1.0)
    scorer: str
    details: dict[str, Any] = Field(default_factory=dict)


class Scorer(Protocol):
    name: str

    def estimate_usd(self, task: BenchTask) -> float:
        """What scoring one answer to ``task`` is expected to cost (0 for deterministic scorers)."""
        ...

    async def score(self, task: BenchTask, answer: AnswerView, env: ScoreEnv) -> ScoreResult: ...


class PointsScorer:
    """Weighted recall of the truth's points, less a penalty for asserting its decoys.

    ``quality = recall - 0.5 * (decoys asserted / decoys)``, never below 0. A point is said when
    any of its keywords appears in the answer or its claims, ignoring case.
    """

    name = "points"
    decoy_penalty = 0.5

    def estimate_usd(self, task: BenchTask) -> float:
        return 0.0

    async def score(self, task: BenchTask, answer: AnswerView, env: ScoreEnv) -> ScoreResult:
        truth = task.parsed_truth()
        if not truth.points:
            return ScoreResult(
                quality=0.0, scorer=self.name, details={"error": "the task has no truth points"}
            )
        text = answer.text().lower()

        def said(keywords: list[str]) -> bool:
            return any(k.lower() in text for k in keywords)

        found = [p.id for p in truth.points if said(p.keywords)]
        asserted = [d.id for d in truth.decoys if said(d.keywords)]
        total = sum(p.weight for p in truth.points)
        recall = sum(p.weight for p in truth.points if p.id in found) / total
        penalty = self.decoy_penalty * len(asserted) / len(truth.decoys) if truth.decoys else 0.0
        return ScoreResult(
            quality=max(recall - penalty, 0.0),
            scorer=self.name,
            details={
                "found": found,
                "missed": [p.id for p in truth.points if p.id not in found],
                "decoys_asserted": asserted,
                "recall": recall,
            },
        )


_POINTS = PointsScorer()
SCORERS: dict[Category, Scorer] = {
    "code_review": _POINTS,
    "debugging": _POINTS,
    "architecture": _POINTS,
    "planning": _POINTS,
    "coding": _POINTS,
    "frontend": _POINTS,
    "performance": _POINTS,
}


def get_scorer(category: Category) -> Scorer:
    return SCORERS[category]
