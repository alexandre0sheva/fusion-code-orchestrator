"""Calibrating the agentic judge: does it pick the better output when we know which it is?

A case is a frontend or performance task with two outputs whose order is known by construction: the
task's reference **solution** and one of its deliberately **flawed** answers. Frontend flaws are
broken pages (a missing label, low contrast, a fixed-width layout that overflows a phone, a modal
that is not a dialog); performance flaws are known-slow solutions or fast-but-wrong ones. Each judge
runs the pairwise protocol of ``agentic_judge`` on every case, both orderings, with the evidence the
evaluators took, and the report gives the same statistics as the text calibration:

* ``accuracy``: cases where both orderings picked the good output;
* ``tie_rate`` and ``inconsistent_rate`` (a different winner in each ordering: position bias);
* ``kappa``: Cohen's κ between the judge's raw A/B/tie calls and the truth, over both orderings;
* accuracy per set (``visual`` for frontend cases, ``perf`` for performance), and agreement and κ
  between judges.

The judge's own call is measured: a hard gate that would decide the winner anyway does not count.
A study whose judge scores below the accuracy floor gets no headline verdict (``judge_gate``).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import combinations
from typing import Literal

from pydantic import BaseModel

from fusion.bench.evaluators import EvaluatorSet
from fusion.bench.patch import PatchError
from fusion.bench.scoring.agentic import DEFAULT_MAX_STEPS, JudgeRun, agentic_judge
from fusion.bench.scoring.artifact import answer_tree, workspace
from fusion.bench.scoring.base import ScoreEnv, ScoringError, estimate_judge_call
from fusion.bench.scoring.calibration import (
    DEFAULT_ACCURACY_FLOOR,
    CalibrationReport,
    JudgeStats,
    cohens_kappa,
)
from fusion.bench.spec import BenchTask, CodingTruth
from fusion.routing.budget import PlannedCall

__all__ = [
    "ArtifactCase",
    "artifact_calibration_calls",
    "artifact_cases",
    "calibrate_artifacts",
]

_PLANNED_STEPS = 8
_STEP_TOKENS = 700
_GOOD, _FLAWED = "good", "flawed"
CaseSet = Literal["visual", "perf"]


class ArtifactCase(BaseModel):
    task_id: str
    flaw: int  # 1-based index into the task's flaws
    set: CaseSet  # "visual" for frontend tasks, "perf" for performance tasks


def artifact_cases(
    tasks: Sequence[BenchTask], *, per_set: int | None = None, seed: int = 0
) -> list[ArtifactCase]:
    """A case for every flaw of every frontend and performance task that has a solution
    (``per_set`` caps each set; the choice is seeded, so the same call gives the same cases)."""
    by_set: dict[CaseSet, list[ArtifactCase]] = {"visual": [], "perf": []}
    for task in tasks:
        if task.category not in ("frontend", "performance") or "expected_pass" not in task.truth:
            continue
        truth = CodingTruth.model_validate(task.truth)
        if not truth.solution:
            continue
        kind: CaseSet = "visual" if task.category == "frontend" else "perf"
        by_set[kind] += [
            ArtifactCase(task_id=task.id, flaw=n, set=kind) for n in range(1, len(truth.flaws) + 1)
        ]
    rng = random.Random(f"artifact-cases:{seed}")  # noqa: S311 — reproducible sampling
    chosen: list[ArtifactCase] = []
    for group in by_set.values():
        picked = group if per_set is None or per_set >= len(group) else rng.sample(group, per_set)
        chosen += sorted(picked, key=lambda c: (c.task_id, c.flaw))
    return chosen


def artifact_calibration_calls(
    tasks: Sequence[BenchTask], cases: Sequence[ArtifactCase], judges: Sequence[str]
) -> list[PlannedCall]:
    """The calls a calibration is expected to make, for pricing: a judge takes about eight steps
    in each of two orderings."""
    by_id = {t.id: t for t in tasks}
    calls: list[PlannedCall] = []
    for case in cases:
        for judge in judges:
            for _order in range(2):
                for step in range(_PLANNED_STEPS):
                    base = estimate_judge_call(
                        by_id[case.task_id], judge, extra_tokens=(step + 1) * _STEP_TOKENS
                    )
                    calls.append(PlannedCall("judge", judge, base.input_tokens, 200))
    return calls


def _picks(runs: Sequence[JudgeRun]) -> list[str | None]:
    """What each ordering chose, as an output name (``good``, ``flawed``) or ``tie``; None when the
    judge gave no verdict in that ordering."""
    picks: list[str | None] = []
    for run in sorted(runs, key=lambda r: r.order):
        if run.ended != "verdict" or run.winner_label is None:
            picks.append(None)
        else:
            picks.append("tie" if run.winner_label == "tie" else run.labels[run.winner_label])
    return picks


def _truth_label(run: JudgeRun) -> str:
    return next(label for label, name in run.labels.items() if name == _GOOD)


async def _judge_case(
    task: BenchTask,
    case: ArtifactCase,
    judges: Sequence[str],
    env: ScoreEnv,
    evaluators: EvaluatorSet,
    seed: int,
    max_steps: int,
) -> dict[str, list[JudgeRun]]:
    truth = CodingTruth.model_validate(task.truth)
    try:
        trees = {
            _GOOD: answer_tree(task, truth.solution),
            _FLAWED: answer_tree(task, truth.flaws[case.flaw - 1].patch),
        }
    except PatchError as exc:
        msg = f"case {case.task_id}/flaw-{case.flaw} does not apply: {exc}"
        raise ScoringError(msg) from exc
    with workspace(trees[_GOOD]) as good_dir, workspace(trees[_FLAWED]) as flawed_dir:
        evidence = {
            _GOOD: await evaluators.collect(good_dir, task),
            _FLAWED: await evaluators.collect(flawed_dir, task),
        }
        verdicts = await asyncio.gather(
            *(
                agentic_judge(
                    task,
                    {_GOOD: good_dir, _FLAWED: flawed_dir},
                    evidence,
                    [judge],
                    "pairwise",
                    env=env,
                    evaluators=evaluators,
                    cross_family=False,  # no arm is being judged
                    min_judges=1,
                    max_steps=max_steps,
                    seed=seed,
                )
                for judge in judges
            )
        )
    return {judge: v.runs for judge, v in zip(judges, verdicts, strict=True)}


def _stats(judge: str, rows: Sequence[Sequence[JudgeRun]]) -> JudgeStats:
    truth: list[str] = []
    called: list[str] = []
    right = ties = flips = failed = 0
    for runs in rows:
        picks = _picks(runs)
        for run, pick in zip(sorted(runs, key=lambda r: r.order), picks, strict=True):
            if pick is None or run.winner_label is None:
                failed += 1
                continue
            truth.append(_truth_label(run))
            called.append(run.winner_label)
        verdict = _verdict(runs)  # a judge that failed an ordering abstains, which is a tie
        right += verdict == _GOOD
        ties += verdict == "tie"
        flips += (
            len(picks) == 2 and None not in picks and picks[0] != picks[1] and "tie" not in picks
        )
    n = len(rows)
    return JudgeStats(
        judge=judge,
        cases=n,
        accuracy=right / n if n else 0.0,
        tie_rate=ties / n if n else 0.0,
        inconsistent_rate=flips / n if n else 0.0,
        kappa=cohens_kappa(truth, called),
        failed_calls=failed,
    )


def _verdict(runs: Sequence[JudgeRun]) -> str:
    picks = _picks(runs)
    if len(picks) == 2 and None not in picks and picks[0] == picks[1] and picks[0] is not None:
        return picks[0]
    return "tie"


async def calibrate_artifacts(
    tasks: Sequence[BenchTask],
    cases: Sequence[ArtifactCase],
    env: ScoreEnv,
    judges: Sequence[str],
    evaluators: EvaluatorSet,
    *,
    seed: int = 0,
    mock: bool = False,
    floor: float = DEFAULT_ACCURACY_FLOOR,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> CalibrationReport:
    """Run every judge over every case and report how well each tells the good output from the
    flawed one, overall and per set."""
    if not judges:
        msg = "calibration needs at least one judge model"
        raise ScoringError(msg)
    if not cases:
        msg = "no frontend or performance task has a solution and a flaw to calibrate on"
        raise ScoringError(msg)
    by_id = {t.id: t for t in tasks}
    unknown = sorted({c.task_id for c in cases if c.task_id not in by_id})
    if unknown:
        msg = f"calibration cases name unknown tasks: {', '.join(unknown)}"
        raise ScoringError(msg)
    spent = env.spent_usd()
    rows: dict[str, list[list[JudgeRun]]] = {j: [] for j in judges}
    for case in cases:  # one case at a time: timings are measured, and a browser is shared
        done = await _judge_case(
            by_id[case.task_id], case, judges, env, evaluators, seed, max_steps
        )
        for judge in judges:
            rows[judge].append(done[judge])
    stats = [_stats(judge, rows[judge]) for judge in judges]
    by_set: dict[str, dict[str, float]] = {}
    for kind in sorted({c.set for c in cases}):
        picked = [i for i, c in enumerate(cases) if c.set == kind]
        by_set[kind] = {
            judge: _stats(judge, [rows[judge][i] for i in picked]).accuracy for judge in judges
        }
    pair_kappa: dict[str, float] = {}
    agreements: list[float] = []
    for first, second in combinations(judges, 2):
        a = [_verdict(runs) for runs in rows[first]]
        b = [_verdict(runs) for runs in rows[second]]
        agreements.append(sum(x == y for x, y in zip(a, b, strict=True)) / len(cases))
        left: list[str] = []
        right: list[str] = []
        for ra, rb in zip(rows[first], rows[second], strict=True):
            for pa, pb in zip(_picks(ra), _picks(rb), strict=True):
                if pa is not None and pb is not None:
                    left.append(pa)
                    right.append(pb)
        pair_kappa[f"{first}|{second}"] = cohens_kappa(left, right)
    now = datetime.now(UTC)
    return CalibrationReport(
        id=now.strftime("cal-%Y%m%d-%H%M%S-artifact"),
        created=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        cases=len(cases),
        judges=stats,
        agreement=sum(agreements) / len(agreements) if agreements else None,
        pair_kappa=pair_kappa,
        cost_usd=env.spent_usd() - spent,
        mock=mock,
        kind="artifact",
        floor=floor,
        by_set=by_set,
    )
