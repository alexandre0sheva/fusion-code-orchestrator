"""``PointsScorer``: the deterministic baseline, and what the other scorers fall back to.

The truth lists ``points`` an answer should say and ``decoys`` it should not; quality is the
weighted share of points found, less a penalty for each decoy asserted.
"""

from __future__ import annotations

from fusion.bench.scoring.base import AnswerView, Evidence, ScoreEnv, ScoreResult, ScoringError
from fusion.bench.spec import BenchTask
from fusion.routing.budget import PlannedCall

__all__ = ["PointsScorer", "points_fallback"]


class PointsScorer:
    """Weighted recall of the truth's points, less a penalty for asserting its decoys.

    ``quality = recall - 0.5 * (decoys asserted / decoys)``, never below 0. A point is said when
    any of its keywords appears in the answer or its claims, ignoring case.
    """

    name = "points"
    decoy_penalty = 0.5

    def estimate_calls(self, task: BenchTask, judges: list[str]) -> list[PlannedCall]:
        return []

    async def score(
        self,
        task: BenchTask,
        answer: AnswerView,
        env: ScoreEnv,
        evidence: Evidence | None = None,
    ) -> ScoreResult:
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


def points_fallback(task: BenchTask, scorer: str) -> PointsScorer:
    """The scorer for a task whose truth is in ``points`` format, where ``scorer`` (the one the
    category names) wants another format. Without points the task cannot be scored at all."""
    if task.truth.get("points"):
        return PointsScorer()
    msg = (
        f"{scorer} cannot score task '{task.id}': its truth has none of the keys it reads "
        f"(it has {', '.join(sorted(task.truth)) or 'none'})"
    )
    raise ScoringError(msg)
