"""``ArtifactScorer``: score a frontend or performance answer by what it built, not by what it said.

The answer's one patch (see ``fusion.bench.patch``) is applied to the task's files and the result is
written to a throwaway directory. The task's evaluators then measure it (hidden tests, build, lint,
timing against the reference, accessibility, a screenshot...) and the evidence is turned into hard
gates and soft criteria (``completion``). When the study names judge models an agentic judge
(``agentic.py``) reads the evidence and the files and scores the criteria; without judges the
criteria are measured and those only a judge could score are left out.

Everything this does besides the judge's model calls is free of model money. The seconds it takes
and the judge's cost are reported as ``eval_seconds`` and ``eval_cost_usd``, never as the arm's.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fusion.bench.evaluators import EvaluatorSet
from fusion.bench.evaluators.base import Evidence as MeasuredEvidence
from fusion.bench.patch import PatchError, apply_patch, extract_patch
from fusion.bench.sandbox import SandboxError, safe_relative_path
from fusion.bench.scoring.agentic import DEFAULT_MAX_STEPS, JudgeVerdict, agentic_judge
from fusion.bench.scoring.base import (
    AnswerView,
    Evidence,
    EvidenceItem,
    ScoreEnv,
    ScoreResult,
    estimate_judge_call,
)
from fusion.bench.scoring.completion import (
    CriterionScore,
    GateResult,
    by_kind,
    combine,
    completion_score,
    criteria_for,
    evaluate_gate,
    gates_for,
)
from fusion.bench.scoring.pairwise import NoEligibleJudgeError
from fusion.bench.spec import BenchTask
from fusion.routing.budget import PlannedCall

__all__ = [
    "ArtifactScorer",
    "Assessment",
    "answer_tree",
    "to_score_evidence",
    "workspace",
]

_STEP_TOKENS = 700  # planning assumption: what each judge step adds to the conversation
_PLANNED_STEPS = 8  # planning assumption: steps a judge takes before submitting
_SHARED: dict[str, EvaluatorSet] = {}


def shared_evaluators() -> EvaluatorSet:
    """The process-wide evaluators (and so their caches) for scoring outside a run."""
    if "default" not in _SHARED:
        _SHARED["default"] = EvaluatorSet()
    return _SHARED["default"]


# -- the answer as files ------------------------------------------------------------------------


def answer_tree(task: BenchTask, patch: str | None) -> dict[str, str]:
    """The task's files with ``patch`` applied (no patch: the files as given). ``PatchError`` when
    the patch cannot be applied."""
    tree = dict(task.files)
    if patch:
        for path, content in apply_patch(task.files, patch).items():
            if content is None:
                tree.pop(path, None)
            else:
                tree[path] = content
    return tree


@contextmanager
def workspace(tree: Mapping[str, str]) -> Iterator[Path]:
    """``tree`` written under a temporary directory, which exists for the ``with`` block."""
    with tempfile.TemporaryDirectory(prefix="fusion-answer-") as raw:
        root = Path(raw) / "answer"
        root.mkdir()
        for rel, text in tree.items():
            target = root / safe_relative_path(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        yield root


def to_score_evidence(evidence: Sequence[MeasuredEvidence]) -> Evidence:
    """Evaluator evidence as the ``Evidence`` the other scorers accept: a line per measurement, so
    a rubric judge can be shown what the tests and timings said."""
    items = [
        EvidenceItem(
            kind=e.kind,
            name=e.name or e.kind,
            passed=e.ok,
            value=next(iter(e.metrics.values()), None) if len(e.metrics) == 1 else None,
            detail=e.summary,
        )
        for e in evidence
    ]
    return Evidence(items=items)


# -- measuring one answer -----------------------------------------------------------------------


@dataclass
class Assessment:
    """Evidence, gates and criteria of one answer, before any judge."""

    evidence: list[MeasuredEvidence]
    gates: list[GateResult]
    criteria: list[CriterionScore]
    completion: float
    seconds: float = 0.0
    verdict: JudgeVerdict | None = None
    notes: list[str] = field(default_factory=list)

    def rescore(self, task: BenchTask, judged: Mapping[str, float] | None) -> None:
        """Recompute criteria and completion with the judge's scores."""
        self.criteria = combine(criteria_for(task), by_kind(self.evidence), judged)
        self.completion = completion_score(self.gates, self.criteria)


async def assess(task: BenchTask, workdir: Path, evaluators: EvaluatorSet) -> Assessment:
    """Run the task's evaluators on ``workdir`` and turn the evidence into gates and criteria."""
    began = time.monotonic()
    evidence = await evaluators.collect(workdir, task)
    kinds = by_kind(evidence)
    gates = [evaluate_gate(g, kinds) for g in gates_for(task)]
    criteria = combine(criteria_for(task), kinds)
    return Assessment(
        evidence=evidence,
        gates=gates,
        criteria=criteria,
        completion=completion_score(gates, criteria),
        seconds=time.monotonic() - began,
    )


class ArtifactScorer:
    """Completion score of a patch answer to a frontend or performance task."""

    name = "artifact"

    def estimate_calls(self, task: BenchTask, judges: list[str]) -> list[PlannedCall]:
        calls: list[PlannedCall] = []
        for alias in judges:
            for step in range(_PLANNED_STEPS):
                base = estimate_judge_call(task, alias, extra_tokens=step * _STEP_TOKENS)
                calls.append(PlannedCall("judge", alias, base.input_tokens, 200))
        return calls

    async def score(
        self,
        task: BenchTask,
        answer: AnswerView,
        env: ScoreEnv,
        evidence: Evidence | None = None,
    ) -> ScoreResult:
        if "expected_pass" not in task.truth:
            from fusion.bench.scoring.points import points_fallback

            return await points_fallback(task, self.name).score(task, answer, env, evidence)
        found = extract_patch(answer.final_answer, answer.structured)
        if found.text is None:
            return ScoreResult(
                quality=0.0,
                scorer=self.name,
                details={"patch": "none", "error": found.problem},
            )
        try:
            tree = answer_tree(task, found.text)
        except (PatchError, SandboxError) as exc:
            return ScoreResult(
                quality=0.0,
                scorer=self.name,
                details={"patch": "rejected", "error": str(exc)},
            )
        began = time.monotonic()
        evaluators = env.evaluators or shared_evaluators()
        with workspace(tree) as workdir:
            result = await assess(task, workdir, evaluators)
            if env.judge_models:
                await self._judge(task, workdir, result, env, evaluators)
        return self._result(task, result, time.monotonic() - began)

    async def _judge(
        self,
        task: BenchTask,
        workdir: Path,
        result: Assessment,
        env: ScoreEnv,
        evaluators: EvaluatorSet,
    ) -> None:
        """Ask the agentic judges to score the criteria; on failure keep the measured scores."""
        try:
            verdict = await agentic_judge(
                task,
                {"answer": workdir},
                {"answer": result.evidence},
                env.judge_models,
                "absolute",
                env=env,
                evaluators=evaluators,
                exclude_providers=env.exclude_providers,
                max_steps=DEFAULT_MAX_STEPS,
            )
        except NoEligibleJudgeError as exc:
            result.notes.append(f"judge skipped: {exc}")
            return
        result.verdict = verdict
        if not verdict.decided:
            result.notes.append("no judge reached a verdict; criteria are the measured ones")
            return
        judged = {
            c.id: c.judged for c in verdict.criteria.get("answer", []) if c.judged is not None
        }
        # Evidence a judge re-ran may be newer than ours: gates stay the harness's own call.
        result.rescore(task, judged)

    def _result(self, task: BenchTask, result: Assessment, seconds: float) -> ScoreResult:
        details: dict[str, object] = {
            "gates": [g.model_dump() for g in result.gates],
            "criteria": [c.model_dump() for c in result.criteria],
            "evidence": [e.line() for e in result.evidence],
        }
        unscored = [c.id for c in result.criteria if c.score is None]
        if unscored:
            details["unscored"] = unscored
        if result.notes:
            details["notes"] = result.notes
        trail: list[dict[str, object]] = []
        cost = 0.0
        if result.verdict is not None:
            v = result.verdict
            details["judge"] = {
                "justification": v.justification,
                "disagreement": v.disagreement,
                "excluded": v.excluded,
                "warnings": v.warnings,
                "steps": [r.steps for r in v.runs],
                "ended": [r.ended for r in v.runs],
            }
            trail = [s.model_dump(mode="json") for s in v.trail]
            cost = v.cost_usd
        return ScoreResult(
            quality=result.completion,
            scorer=self.name,
            details=details,
            cost_usd=cost,
            evidence=[e.model_dump(mode="json") for e in result.evidence],
            trail=trail,
            eval_seconds=seconds,
        )
