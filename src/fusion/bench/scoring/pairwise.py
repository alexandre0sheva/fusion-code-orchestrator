"""``PairwiseJudge``: which of two answers is better, with the known biases of judges removed.

* **Both orderings.** Every judge sees (A, B) and then (B, A). A judge that names the same answer
  both times has a verdict; one that names a different answer each time (it follows the position)
  has none, and counts as a tie, so position bias cancels instead of deciding the result.
* **Ties.** A judge may call a tie; so does a judge whose two orderings disagree.
* **Cross-family rule.** A judge must not come from a provider that serves either arm, since
  models favour their own family's answers. Pass the arms' providers as ``exclude_providers``;
  ``cross_family=False`` turns the rule off. With no eligible judge the comparison raises
  ``NoEligibleJudgeError`` rather than quietly using a conflicted one.
* **Several judges** each give a verdict and the answer with more votes wins; equal votes tie.

Judges are catalog aliases from ``ScoreEnv.judge_models``; their cost is the scorer's, not an arm's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, Field

from fusion.bench.scoring.base import ScoreEnv, ScoringError, ask_judge, task_text
from fusion.bench.spec import BenchTask
from fusion.config.catalog import ModelEntry

__all__ = [
    "NoEligibleJudgeError",
    "PairwiseJudge",
    "PairwiseResult",
    "Side",
    "Verdict",
    "eligible_judges",
    "providers_of",
]

Side = Literal["a", "b", "tie"]


class NoEligibleJudgeError(ScoringError):
    """Every judge model is excluded by the cross-family rule (or none was named)."""


class Verdict(BaseModel):
    """One judge's call, from both orderings."""

    judge: str
    winner: Side  # "tie" also when the two orderings disagreed
    first: Side | None  # the call with the answers as given; None when the judge failed
    swapped: Side | None  # the call with them swapped, mapped back to the original sides
    position_inconsistent: bool = False


class PairwiseResult(BaseModel):
    winner: Side
    votes: list[Verdict] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)  # judges the cross-family rule removed
    cost_usd: float = 0.0

    @property
    def value(self) -> float:
        """1 when A wins, 0 when B wins, 0.5 for a tie."""
        return {"a": 1.0, "b": 0.0, "tie": 0.5}[self.winner]


def providers_of(aliases: Iterable[str], models: Mapping[str, ModelEntry]) -> set[str]:
    """The providers behind some catalog aliases (an arm's members and aggregator)."""
    return {models[a].provider for a in aliases if a in models}


def eligible_judges(
    judges: list[str],
    models: Mapping[str, ModelEntry],
    exclude_providers: set[str],
    *,
    cross_family: bool = True,
) -> tuple[list[str], list[str]]:
    """``(usable judges, judges removed by the cross-family rule)``."""
    if not cross_family:
        return list(judges), []
    usable = [j for j in judges if j in models and models[j].provider not in exclude_providers]
    return usable, [j for j in judges if j not in usable]


_FLIPPED: dict[Side, Side] = {"a": "b", "b": "a", "tie": "tie"}


def _flip(side: Side) -> Side:
    return _FLIPPED[side]


def _side(raw: object) -> Side | None:
    names: dict[str, Side] = {"a": "a", "b": "b", "tie": "tie", "draw": "tie", "equal": "tie"}
    return names.get(str(raw).strip().lower())


class PairwiseJudge:
    def __init__(self, *, cross_family: bool = True) -> None:
        self.cross_family = cross_family

    async def compare(
        self,
        task: BenchTask,
        answer_a: str,
        answer_b: str,
        env: ScoreEnv,
        *,
        exclude_providers: set[str] | None = None,
        judges: list[str] | None = None,
    ) -> PairwiseResult:
        """Compare two answers to ``task``. ``judges`` defaults to ``env.judge_models``."""
        spent = env.spent_usd()
        usable, excluded = eligible_judges(
            judges if judges is not None else env.judge_models,
            env.gateway.models,
            exclude_providers or set(),
            cross_family=self.cross_family,
        )
        if not usable:
            why = (
                f"every judge ({', '.join(excluded)}) shares a provider with an arm"
                if excluded
                else "no judge models are named"
            )
            raise NoEligibleJudgeError(f"cannot compare answers to '{task.id}': {why}")
        verdicts = await asyncio.gather(
            *(self.judge_one(task, answer_a, answer_b, env, j) for j in usable)
        )
        a_votes = sum(v.winner == "a" for v in verdicts)
        b_votes = sum(v.winner == "b" for v in verdicts)
        winner: Side = "a" if a_votes > b_votes else "b" if b_votes > a_votes else "tie"
        return PairwiseResult(
            winner=winner,
            votes=list(verdicts),
            excluded=excluded,
            cost_usd=env.spent_usd() - spent,
        )

    async def judge_one(
        self, task: BenchTask, answer_a: str, answer_b: str, env: ScoreEnv, judge: str
    ) -> Verdict:
        """One judge, both orderings. A failed call leaves that ordering out, and a verdict
        needs both, so the judge then abstains (a tie)."""
        first, second = await asyncio.gather(
            self._ask(task, answer_a, answer_b, env, judge),
            self._ask(task, answer_b, answer_a, env, judge),
        )
        swapped = _flip(second) if second is not None else None
        if first is None or swapped is None:
            return Verdict(judge=judge, winner="tie", first=first, swapped=swapped)
        agree = first == swapped
        return Verdict(
            judge=judge,
            winner=first if agree else "tie",
            first=first,
            swapped=swapped,
            position_inconsistent=not agree and "tie" not in (first, swapped),
        )

    async def _ask(
        self, task: BenchTask, shown_a: str, shown_b: str, env: ScoreEnv, judge: str
    ) -> Side | None:
        prompt = (
            "Two answers to the same task follow. Decide which is better: more correct, more "
            "specific to the task, and more useful to the person who asked. Do not prefer an "
            "answer for its length, its position or its tone. If they are equally good, say "
            "tie.\n\n"
            f"Task:\n{task_text(task)}\n\n=== ANSWER A ===\n{shown_a}\n\n=== ANSWER B ===\n"
            f'{shown_b}\n\nAnswer: {{"winner": "A" | "B" | "tie", "reason": "..."}}'
        )
        data = await ask_judge(
            env,
            judge,
            kind="pairwise",
            prompt=prompt,
            payload={"task": task.prompt, "a": shown_a, "b": shown_b},
            max_tokens=600,
        )
        return _side(data.get("winner")) if data else None
