"""``DebugScorer``: did the answer name the root cause, and say how to fix it.

The truth's ``root_cause_tags`` are equivalent canonical names for the one root cause (with
``root_cause_aliases`` for other wordings). The answer's hypotheses are its list entries (its
paragraphs when it has no list), in the order it gives them; the rank of the first hypothesis
that names the cause is what is scored: first place earns full credit, second or third 0.7, a
later mention 0.3, none 0. When no hypothesis names it in so many words, the first judge model is
asked whether one of the first five says the same thing in other words (``root_cause`` is the
cause in a sentence for it to compare against). The fix is the share of ``fix_keywords`` the
answer mentions (``a|b`` accepts either). ``quality = 0.7 * cause + 0.3 * fix``, or the cause
alone when the task lists no fix keywords.
"""

from __future__ import annotations

from typing import Any

from fusion.bench.scoring.base import (
    AnswerView,
    Evidence,
    ScoreEnv,
    ScoreResult,
    ask_judge,
    estimate_judge_call,
    normalize,
    segments,
)
from fusion.bench.scoring.points import points_fallback
from fusion.bench.spec import BenchTask, DebugTruth
from fusion.routing.budget import PlannedCall
from fusion.security.untrusted import wrap_untrusted

__all__ = ["DebugScorer", "rank_credit"]

CAUSE_WEIGHT = 0.7
_JUDGED_HYPOTHESES = 5


def rank_credit(rank: int | None) -> float:
    """Credit for the cause appearing as the ``rank``-th hypothesis (from 1)."""
    if rank is None:
        return 0.0
    return 1.0 if rank == 1 else 0.7 if rank <= 3 else 0.3


def _names(truth: DebugTruth) -> list[str]:
    names = [*truth.root_cause_tags]
    for tag in truth.root_cause_tags:
        names.extend(truth.root_cause_aliases.get(tag, []))
    return [normalize(n) for n in names if n.strip()]


def _mentions(text: str, alternatives: str) -> bool:
    haystack = normalize(text)
    return any(normalize(a) in haystack for a in alternatives.split("|") if a.strip())


class DebugScorer:
    """Root-cause rank (top-1 / top-3) plus fix keywords; see the module doc."""

    name = "debug"

    def estimate_calls(self, task: BenchTask, judges: list[str]) -> list[PlannedCall]:
        if "root_cause_tags" not in task.truth or not judges:
            return []
        return [estimate_judge_call(task, judges[0])]

    async def score(
        self,
        task: BenchTask,
        answer: AnswerView,
        env: ScoreEnv,
        evidence: Evidence | None = None,
    ) -> ScoreResult:
        if "root_cause_tags" not in task.truth:
            return await points_fallback(task, self.name).score(task, answer, env, evidence)
        truth = DebugTruth.model_validate(task.truth)
        spent = env.spent_usd()
        hypotheses = segments(answer.text())
        names = _names(truth)
        rank = next(
            (i for i, h in enumerate(hypotheses, 1) if any(n in normalize(h) for n in names)),
            None,
        )
        method = "tag" if rank is not None else None
        if rank is None and hypotheses and env.judge_models:
            rank = await self._judge_rank(task, truth, hypotheses, env)
            method = "judge" if rank is not None else None
        cause = rank_credit(rank)

        text = answer.text()
        found = [k for k in truth.fix_keywords if _mentions(text, k)]
        fix = len(found) / len(truth.fix_keywords) if truth.fix_keywords else None
        quality = cause if fix is None else CAUSE_WEIGHT * cause + (1 - CAUSE_WEIGHT) * fix
        details: dict[str, Any] = {
            "rank": rank,
            "method": method,
            "top1": rank == 1,
            "top3": rank is not None and rank <= 3,
            "cause_credit": cause,
            "hypotheses": len(hypotheses),
            "fix_found": found,
            "fix_missed": [k for k in truth.fix_keywords if k not in found],
            "fix_credit": fix,
        }
        if env.errors:
            details["judge_errors"] = list(env.errors)
        return ScoreResult(
            quality=quality, scorer=self.name, details=details, cost_usd=env.spent_usd() - spent
        )

    async def _judge_rank(
        self, task: BenchTask, truth: DebugTruth, hypotheses: list[str], env: ScoreEnv
    ) -> int | None:
        """The rank of the first hypothesis the judge calls the same cause, if any."""
        cause = truth.root_cause or " / ".join(truth.root_cause_tags)
        shown = [
            {"id": i, "text": h[:600]} for i, h in enumerate(hypotheses[:_JUDGED_HYPOTHESES], 1)
        ]
        listing = "\n".join(f"{c['id']}. {c['text']}" for c in shown)
        prompt = (
            f"The true root cause of the bug is: {cause}\n\nA reviewer proposed these "
            f"hypotheses, in order:\n{wrap_untrusted(listing)}\n\n"
            "Which hypotheses state the same root cause "
            "(the same mechanism, in any words)? Naming a symptom or a different mechanism does "
            'not count.\nAnswer: {"equivalent": [ids]}'
        )
        data = await ask_judge(
            env,
            env.judge_models[0],
            kind="equivalence",
            prompt=prompt,
            payload={"cause": cause, "candidates": shown},
        )
        ids = [i for i in (data or {}).get("equivalent", []) if isinstance(i, int)]
        valid = [i for i in ids if 1 <= i <= len(shown)]
        return min(valid) if valid else None
