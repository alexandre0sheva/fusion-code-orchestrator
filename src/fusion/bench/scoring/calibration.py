"""Judge calibration: how often does a judge pick the better answer when we know which it is?

A case is a task with two answers whose quality order is known by construction: a *good* answer
that says what the task's truth says, and a *flawed* one that misses most of it and asserts what
is known to be wrong. ``seeded_pair`` writes both from the truth (or a cases file supplies them).
Each judge compares every pair in both orderings (``PairwiseJudge.judge_one``) and the report
gives, per judge:

* ``accuracy``: pairs where both orderings picked the good answer;
* ``tie_rate`` and ``inconsistent_rate`` (a different winner in each ordering: position bias);
* ``kappa``: Cohen's κ between the judge's raw A/B/tie calls and the truth, over both orderings.
  Truth is A in one ordering and B in the other, so a judge that always says "A" scores κ ≈ 0
  however accurate it looks on one ordering.

and between judges ``agreement`` (pairs given the same final verdict) and a κ per pair of judges.
Reports are stored under ``<bench-results>/calibration/`` and printed by the bench reports.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from fusion.bench.scoring.base import ScoreEnv, ScoringError, estimate_judge_call
from fusion.bench.scoring.pairwise import PairwiseJudge, Side, Verdict
from fusion.bench.spec import BenchTask, DebugTruth, ReviewTruth, RubricTruth
from fusion.routing.budget import PlannedCall

__all__ = [
    "CalibrationCase",
    "ACCURACY_FLOOR_ENV",
    "DEFAULT_ACCURACY_FLOOR",
    "CalibrationReport",
    "JudgeGate",
    "JudgeStats",
    "build_cases",
    "calibrate",
    "calibration_calls",
    "cases_from_file",
    "cohens_kappa",
    "judge_floor",
    "judge_gate",
    "load_reports",
    "save_report",
    "seeded_pair",
]

_CALIBRATION_DIR = "calibration"
ACCURACY_FLOOR_ENV = "FUSION_JUDGE_ACCURACY_FLOOR"
DEFAULT_ACCURACY_FLOOR = 0.8
_EXTRA_ANSWER_TOKENS = 1200  # the second answer a pairwise prompt carries


class CalibrationCase(BaseModel):
    task_id: str
    good: str
    flawed: str


class JudgeStats(BaseModel):
    judge: str
    cases: int
    accuracy: float
    tie_rate: float
    inconsistent_rate: float
    kappa: float  # the judge's raw calls against the truth, both orderings
    failed_calls: int = 0  # orderings that returned nothing usable (left out of kappa)

    def meets(self, floor: float) -> bool:
        return self.accuracy >= floor


class CalibrationReport(BaseModel):
    id: str
    created: str
    cases: int
    judges: list[JudgeStats]
    agreement: float | None = None  # mean over judge pairs of the share of cases they agree on
    pair_kappa: dict[str, float] = Field(default_factory=dict)  # "judge-a|judge-b" -> κ
    cost_usd: float = 0.0
    mock: bool = False
    # "artifact" reports calibrate the agentic judge on frontend and performance outputs
    # (good solutions against deliberately flawed ones); they are what the headline gate reads.
    kind: Literal["text", "artifact"] = "text"
    floor: float = DEFAULT_ACCURACY_FLOOR
    by_set: dict[str, dict[str, float]] = Field(default_factory=dict)  # set -> judge -> accuracy


def judge_floor() -> float:
    """The accuracy a judge must reach for a headline verdict, from ``FUSION_JUDGE_ACCURACY_FLOOR``
    (default 0.8)."""
    raw = os.environ.get(ACCURACY_FLOOR_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_ACCURACY_FLOOR
    except ValueError:
        return DEFAULT_ACCURACY_FLOOR
    return value if 0.0 <= value <= 1.0 else DEFAULT_ACCURACY_FLOOR


class JudgeGate(BaseModel):
    """Whether the judges a study used are trustworthy enough for a headline verdict."""

    blocked: bool
    floor: float
    reasons: list[str] = Field(default_factory=list)
    accuracy: dict[str, float | None] = Field(default_factory=dict)  # judge -> latest accuracy


def judge_gate(
    judges: list[str], reports: list[CalibrationReport], floor: float | None = None
) -> JudgeGate:
    """Block the headline verdict when a judge has no artifact calibration, or when its latest one
    is below ``floor``. ``reports`` should be those of the same kind of run (simulated or live)."""
    need = judge_floor() if floor is None else floor
    gate = JudgeGate(blocked=False, floor=need)
    artifact = [r for r in reports if r.kind == "artifact"]
    for judge in judges:
        latest = next(
            (st for r in reversed(artifact) for st in r.judges if st.judge == judge), None
        )
        gate.accuracy[judge] = latest.accuracy if latest else None
        if latest is None:
            gate.reasons.append(
                f"judge {judge} has not been calibrated (fusion bench calibrate-judge --artifacts)"
            )
        elif not latest.meets(need):
            gate.reasons.append(
                f"judge {judge} picked the better output {latest.accuracy:.0%} of the time, "
                f"below the {need:.0%} floor"
            )
    gate.blocked = bool(gate.reasons)
    return gate


def cohens_kappa(first: list[str], second: list[str]) -> float:
    """Cohen's κ for two raters' labels of the same items.

    When both raters always use one and the same label, chance agreement is 1 and κ is undefined;
    this returns 1.0 for full agreement and 0.0 otherwise.
    """
    if len(first) != len(second):
        msg = "both raters must label the same items"
        raise ValueError(msg)
    n = len(first)
    if n == 0:
        return 0.0
    observed = sum(a == b for a, b in zip(first, second, strict=True)) / n
    ca, cb = Counter(first), Counter(second)
    chance = sum(ca[label] * cb[label] for label in set(ca) | set(cb)) / (n * n)
    if chance >= 1.0 - 1e-12:
        return 1.0 if observed >= 1.0 - 1e-12 else 0.0
    return (observed - chance) / (1.0 - chance)


# -- the pairs ---------------------------------------------------------------------------------


def seeded_pair(task: BenchTask) -> tuple[str, str] | None:
    """``(good, flawed)`` answers written from the task's truth; None if its truth has no shape
    this knows. The good answer says everything the truth wants; the flawed one says little of it
    and asserts what the truth calls wrong (or, with nothing to call wrong, invents a defect)."""
    truth = task.truth
    if truth.get("points"):
        parsed = task.parsed_truth()
        good = "\n".join(f"- {p.text}" for p in parsed.points)
        wrong = [d.text for d in parsed.decoys]
        kept = [parsed.points[0].text] if len(parsed.points) > 2 else []
        flawed = "\n".join(f"- {t}" for t in [*kept, *wrong]) or "No problems found."
        return good, flawed
    if "bugs" in truth:
        review = ReviewTruth.model_validate(truth)
        if not review.bugs:
            return (
                "No issues found: the change is correct and safe to merge.",
                "- nonexistent/handler.py:10 possible null dereference when the config is missing",
            )
        good = "\n".join(f"- {b.file}:{b.line} {b.category}: {b.description}" for b in review.bugs)
        flawed = "\n".join(
            f"- {b.file}:{b.line + 40} style: rename the variable for readability"
            for b in review.bugs
        )
        return good, flawed
    if "root_cause_tags" in truth:
        debug = DebugTruth.model_validate(truth)
        cause = debug.root_cause or debug.root_cause_tags[0]
        fix = f"\nFix: {', '.join(debug.fix_keywords)}." if debug.fix_keywords else ""
        good = f"1. Root cause: {debug.root_cause_tags[0]}. {cause}{fix}"
        flawed = (
            "1. The network is probably flaky; add a retry around the call.\n"
            "2. The cache may be stale; clear it and try again."
        )
        return good, flawed
    if {"required_points", "forbidden_points"} & truth.keys():
        rubric = RubricTruth.model_validate(truth)
        if not rubric.required_points:
            return None
        good = "\n".join(f"- {i.statement()}" for i in rubric.required_points)
        bad = [i.statement() for i in rubric.forbidden_points]
        flawed = "\n".join(f"- {t}" for t in [rubric.required_points[0].statement(), *bad])
        return good, flawed
    return None


def build_cases(tasks: list[BenchTask]) -> list[CalibrationCase]:
    """A seeded good-versus-flawed case for every task that has one."""
    cases = []
    for task in tasks:
        pair = seeded_pair(task)
        if pair and pair[0] != pair[1]:
            cases.append(CalibrationCase(task_id=task.id, good=pair[0], flawed=pair[1]))
    return cases


def cases_from_file(path: Path) -> list[CalibrationCase]:
    """Cases written by hand: JSONL rows of ``{"task_id", "good", "flawed"}``."""
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return [CalibrationCase.model_validate(r) for r in rows]


def calibration_calls(
    tasks: list[BenchTask], cases: list[CalibrationCase], judges: list[str]
) -> list[PlannedCall]:
    """The calls a calibration makes, for pricing: two per judge per case."""
    by_id = {t.id: t for t in tasks}
    return [
        estimate_judge_call(by_id[c.task_id], judge, extra_tokens=_EXTRA_ANSWER_TOKENS)
        for c in cases
        for judge in judges
        for _ in range(2)
    ]


# -- running it --------------------------------------------------------------------------------


def _raw_calls(verdict: Verdict) -> list[tuple[str, str]]:
    """``(truth, the judge's call)`` for each ordering that returned one. The good answer was A
    in the first ordering and B in the second, whose call is stored mapped back to the first's."""
    calls: list[tuple[str, str]] = []
    if verdict.first is not None:
        calls.append(("a", verdict.first))
    if verdict.swapped is not None:
        calls.append(("b", _unmap(verdict.swapped)))
    return calls


def _unmap(side: Side) -> str:
    return {"a": "b", "b": "a", "tie": "tie"}[side]


async def calibrate(
    tasks: list[BenchTask],
    cases: list[CalibrationCase],
    env: ScoreEnv,
    judges: list[str],
    *,
    concurrency: int = 8,
    mock: bool = False,
) -> CalibrationReport:
    """Run every judge over every case and report how well each one tells good from flawed."""
    if not judges:
        msg = "calibration needs at least one judge model"
        raise ScoringError(msg)
    if not cases:
        msg = "calibration has no cases: no task has truth to seed good and flawed answers from"
        raise ScoringError(msg)
    by_id = {t.id: t for t in tasks}
    unknown = sorted({c.task_id for c in cases if c.task_id not in by_id})
    if unknown:
        msg = f"calibration cases name unknown tasks: {', '.join(unknown)}"
        raise ScoringError(msg)
    spent = env.spent_usd()
    judge = PairwiseJudge(cross_family=False)  # no arms are being compared
    gate = asyncio.Semaphore(concurrency)

    async def one(alias: str, case: CalibrationCase) -> Verdict:
        async with gate:
            return await judge.judge_one(by_id[case.task_id], case.good, case.flawed, env, alias)

    verdicts: dict[str, list[Verdict]] = {}
    for alias in judges:
        verdicts[alias] = list(await asyncio.gather(*(one(alias, c) for c in cases)))

    stats: list[JudgeStats] = []
    for alias, rows in verdicts.items():
        truth, called = [], []
        for v in rows:
            for t, c in _raw_calls(v):
                truth.append(t)
                called.append(c)
        n = len(rows)
        stats.append(
            JudgeStats(
                judge=alias,
                cases=n,
                accuracy=sum(v.winner == "a" for v in rows) / n,
                tie_rate=sum(v.winner == "tie" for v in rows) / n,
                inconsistent_rate=sum(v.position_inconsistent for v in rows) / n,
                kappa=cohens_kappa(truth, called),
                failed_calls=2 * n - len(called),
            )
        )

    pair_kappa: dict[str, float] = {}
    agreements: list[float] = []
    for first, second in combinations(judges, 2):
        agreements.append(
            sum(
                a.winner == b.winner for a, b in zip(verdicts[first], verdicts[second], strict=True)
            )
            / len(cases)
        )
        # κ over the orderings both judges answered: rebuild the aligned per-ordering calls.
        left, right = [], []
        for a, b in zip(verdicts[first], verdicts[second], strict=True):
            ca, cb = dict(_raw_calls(a)), dict(_raw_calls(b))
            for ordering in ("a", "b"):
                if ordering in ca and ordering in cb:
                    left.append(ca[ordering])
                    right.append(cb[ordering])
        pair_kappa[f"{first}|{second}"] = cohens_kappa(left, right)
    return CalibrationReport(
        id=datetime.now(UTC).strftime("cal-%Y%m%d-%H%M%S"),
        created=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        cases=len(cases),
        judges=stats,
        agreement=sum(agreements) / len(agreements) if agreements else None,
        pair_kappa=pair_kappa,
        cost_usd=env.spent_usd() - spent,
        mock=mock,
    )


# -- keeping reports ---------------------------------------------------------------------------


def save_report(report: CalibrationReport, root: Path) -> Path:
    folder = root / _CALIBRATION_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{report.id}.json"
    suffix = 1
    while path.exists():  # two calibrations in one second
        suffix += 1
        path = folder / f"{report.id}-{suffix}.json"
    path.write_text(report.model_dump_json(indent=1) + "\n", encoding="utf-8")
    return path


def load_reports(root: Path) -> list[CalibrationReport]:
    """Every stored report, oldest first (by creation time, then by when the file was written, so
    two reports saved in the same second keep their order)."""
    folder = root / _CALIBRATION_DIR
    if not folder.is_dir():
        return []
    found = [
        (CalibrationReport.model_validate_json(p.read_text(encoding="utf-8")), p.stat().st_mtime_ns)
        for p in folder.glob("*.json")
    ]
    return [r for r, _ in sorted(found, key=lambda pair: (pair[0].created, pair[1]))]
