"""Scorers: ground truth first, an LLM judge second. See ``base`` for the interface.

One scorer is registered per category in ``SCORERS``; each reads the truth format its category's
datasets use and falls back to ``PointsScorer`` for a task whose truth is in ``points`` format.
"""

from __future__ import annotations

from fusion.bench.scoring.artifact import ArtifactScorer
from fusion.bench.scoring.base import (
    PASS_THRESHOLDS,
    AnswerView,
    Evidence,
    EvidenceItem,
    ScoreEnv,
    Scorer,
    ScoreResult,
    ScoringError,
    is_solved,
)
from fusion.bench.scoring.coding import CodingScorer
from fusion.bench.scoring.debug import DebugScorer
from fusion.bench.scoring.pairwise import NoEligibleJudgeError, PairwiseJudge, PairwiseResult
from fusion.bench.scoring.points import PointsScorer
from fusion.bench.scoring.review import ReviewScorer
from fusion.bench.scoring.rubric import RubricScorer
from fusion.bench.spec import Category

__all__ = [
    "PASS_THRESHOLDS",
    "SCORERS",
    "AnswerView",
    "ArtifactScorer",
    "CodingScorer",
    "DebugScorer",
    "Evidence",
    "EvidenceItem",
    "NoEligibleJudgeError",
    "PairwiseJudge",
    "PairwiseResult",
    "PointsScorer",
    "ReviewScorer",
    "RubricScorer",
    "ScoreEnv",
    "ScoreResult",
    "Scorer",
    "ScoringError",
    "get_scorer",
    "is_solved",
]

_POINTS = PointsScorer()
_RUBRIC = RubricScorer()
# Coding is scored by running its hidden tests (``CodingScorer``). Frontend and performance tasks
# are scored on what their answers build (``ArtifactScorer``: evaluators, hard gates and weighted
# criteria, with an agentic judge when the study names judges); a task of either category written
# in ``points`` format still falls back to that scorer.
SCORERS: dict[Category, Scorer] = {
    "code_review": ReviewScorer(),
    "debugging": DebugScorer(),
    "architecture": _RUBRIC,
    "planning": _RUBRIC,
    "coding": CodingScorer(),
    "frontend": ArtifactScorer(),
    "performance": ArtifactScorer(),
}


def get_scorer(category: Category) -> Scorer:
    return SCORERS[category]
