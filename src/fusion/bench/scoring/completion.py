"""From evidence to a completion score: hard gates, measured criteria and their arithmetic.

A frontend or performance task holds an answer to **hard gates** (facts that must be true, such as
"the hidden tests pass") and weighs **soft criteria** (correctness, performance, accessibility,
design, code quality). The **completion score** is 0 when a gate fails and otherwise the weighted
mean of the criteria, so a page that looks lovely and breaks the tests completes nothing.

Most criteria can be *measured* from evidence with no judge (``measured_score``); the rest need a
judge (``source: judge``). Both are used together: a judge scores every criterion after reading the
evidence, and a criterion the judge did not score falls back to its measurement. Evidence that could
not be taken (no browser installed, a noisy timing) leaves its gate *unverified*, which does not
fail it, and its criterion unscored, which leaves it out of the mean instead of counting as zero.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from pydantic import BaseModel

from fusion.bench.evaluators.base import Evidence
from fusion.bench.spec import BenchTask, Criterion, Gate

__all__ = [
    "CriterionScore",
    "GateResult",
    "by_kind",
    "combine",
    "completion_score",
    "criteria_for",
    "default_criteria",
    "default_gates",
    "evaluate_gate",
    "gates_for",
    "measured_score",
    "weighted_mean",
]

# Penalty weights of the static, accessibility and perf measures. They are judgement calls, kept
# here in one place and documented in BENCHMARKING.md; the benchmark's results move with them.
_A11Y_WEIGHT = {"critical": 1.0, "serious": 0.5, "moderate": 0.2, "minor": 0.05}
_A11Y_BUDGET = 3.0  # weighted violations at which the accessibility score reaches zero
_PERF_FULL_RATIO = 1.25  # within this of the reference: full marks
_PERF_ZERO_RATIO = 10.0  # this many times slower: none
_PERF_SCALING_FREE = 0.25  # exponent above the reference's that costs nothing
_PERF_SCALING_ZERO = 1.25  # ...and the excess at which the scaling score reaches zero


class GateResult(BaseModel):
    id: str
    passed: bool | None  # None: unverified (the evidence could not be taken)
    detail: str = ""
    evidence_id: str = ""


class CriterionScore(BaseModel):
    id: str
    weight: float
    score: float | None  # None: not scored by anything available
    basis: str  # "judge", "measured" or "unscored"
    measured: float | None = None
    judged: float | None = None


def default_gates(task: BenchTask) -> list[Gate]:
    """Gates for a task that names none: the hidden tests pass (and, for performance, speed)."""
    gates = [Gate(id="tests-pass", evidence="tests", description="the hidden tests pass")]
    if task.category == "performance":
        gates.append(Gate(id="fast-enough", evidence="perf", description="within the time budget"))
    return gates


def default_criteria(task: BenchTask) -> list[Criterion]:
    if task.category == "performance":
        return [
            Criterion(id="correctness", weight=3, source="tests"),
            Criterion(id="performance", weight=3, source="perf"),
            Criterion(id="code_quality", weight=1, source="static"),
        ]
    return [
        Criterion(id="correctness", weight=3, source="tests"),
        Criterion(id="accessibility", weight=2, source="a11y"),
        Criterion(id="responsiveness", weight=1.5, source="visual"),
        Criterion(id="design_fidelity", weight=2, source="judge"),
        Criterion(id="ux_polish", weight=1, source="judge"),
        Criterion(id="code_quality", weight=1, source="static"),
    ]


def gates_for(task: BenchTask) -> list[Gate]:
    return task.artifact_truth().hard_gates or default_gates(task)


def criteria_for(task: BenchTask) -> list[Criterion]:
    return task.artifact_truth().soft_criteria or default_criteria(task)


# -- gates ------------------------------------------------------------------------------------


def by_kind(evidence: Sequence[Evidence]) -> dict[str, Evidence]:
    """The evidence of each kind (the last one when a kind was taken twice)."""
    return {e.kind: e for e in evidence}


def evaluate_gate(gate: Gate, evidence: Mapping[str, Evidence]) -> GateResult:
    found = evidence.get(gate.evidence)
    if found is None:
        return GateResult(id=gate.id, passed=None, detail=f"no {gate.evidence} evidence")
    if found.status != "measured":
        return GateResult(
            id=gate.id,
            passed=None,
            detail=f"unverified: {found.summary}"[:200],
            evidence_id=found.id,
        )
    if gate.metric is None:
        if found.ok is None:
            return GateResult(id=gate.id, passed=None, detail="no verdict", evidence_id=found.id)
        return GateResult(
            id=gate.id,
            passed=found.ok,
            detail="" if found.ok else found.summary[:200],
            evidence_id=found.id,
        )
    value = found.metrics.get(gate.metric)
    if value is None:
        return GateResult(
            id=gate.id,
            passed=None,
            detail=f"{gate.evidence} has no metric {gate.metric}",
            evidence_id=found.id,
        )
    low = gate.min is not None and value < gate.min
    high = gate.max is not None and value > gate.max
    bounds = " and ".join(
        f"{op} {bound:g}" for op, bound in (("<=", gate.max), (">=", gate.min)) if bound is not None
    )
    return GateResult(
        id=gate.id,
        passed=not (low or high),
        detail=f"{gate.metric} = {value:g}, needs {bounds}",
        evidence_id=found.id,
    )


# -- measured criteria -------------------------------------------------------------------------


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _static_score(e: Evidence) -> float:
    m = e.metrics
    penalty = (
        0.1 * m.get("lint_issues", 0.0)
        + 0.03 * max(0.0, m.get("max_complexity", 0.0) - 10.0)
        + 0.5 * m.get("secrets", 0.0)
        + 0.15 * m.get("new_dependencies", 0.0)
        + 0.2 * m.get("external_requests", 0.0)
        + (0.15 if m.get("html_files") and not m.get("has_viewport_meta") else 0.0)
    )
    return _clamp(1.0 - penalty)


def _a11y_score(e: Evidence) -> float:
    weighted = sum(_A11Y_WEIGHT[k] * e.metrics.get(k, 0.0) for k in _A11Y_WEIGHT)
    return _clamp(1.0 - weighted / _A11Y_BUDGET)


def _perf_score(e: Evidence) -> float | None:
    if e.status != "measured":
        return None
    ratio = e.metrics.get("ratio_vs_reference")
    if ratio is None:
        return None
    if ratio <= _PERF_FULL_RATIO:
        speed = 1.0
    else:
        speed = 1.0 - math.log(ratio / _PERF_FULL_RATIO) / math.log(
            _PERF_ZERO_RATIO / _PERF_FULL_RATIO
        )
    excess = e.metrics.get("scaling_excess", 0.0)
    scaling = 1.0 - (excess - _PERF_SCALING_FREE) / (_PERF_SCALING_ZERO - _PERF_SCALING_FREE)
    return _clamp(min(speed, scaling if excess > _PERF_SCALING_FREE else 1.0))


def _visual_score(shot: Evidence | None, console: Evidence | None) -> float | None:
    """The share of the page's basic checks that hold: loads, shows text, fits a phone, logs no
    errors and fetches nothing it should not."""
    if shot is None or shot.status != "measured":
        return None
    m = shot.metrics
    checks = [
        m.get("loads", 0.0) == 1.0,
        m.get("text_chars", 0.0) > 0,
        m.get("overflow_x_mobile_px", 0.0) <= 1.0,
    ]
    if console is not None and console.status == "measured":
        checks.append(console.metrics.get("console_errors", 0.0) == 0)
        checks.append(console.metrics.get("failed_requests", 0.0) == 0)
    return sum(checks) / len(checks)


def measured_score(criterion: Criterion, evidence: Mapping[str, Evidence]) -> float | None:
    """What the evidence says about ``criterion``, or None when it cannot say."""
    source = criterion.source
    if source == "judge":
        return None
    if source == "visual":
        return _visual_score(evidence.get("screenshot"), evidence.get("console"))
    kind = {"tests": "tests", "static": "static", "perf": "perf", "a11y": "a11y"}[source]
    found = evidence.get(kind)
    if found is None or found.status == "skipped":
        return None
    if source == "tests":
        return found.metrics.get("pass_fraction")
    if source == "static":
        return _static_score(found)
    if source == "a11y":
        score = _a11y_score(found)
        # Source-only rules see less than a browser does: a clean result is not full credit.
        return score if found.metrics.get("engine", 1.0) else min(score, 0.9)
    return _perf_score(found)


# -- the arithmetic ---------------------------------------------------------------------------


def weighted_mean(parts: Sequence[tuple[float, float | None]]) -> float | None:
    """Mean of ``(weight, score)`` pairs, leaving out unscored ones (None if none is scored)."""
    scored = [(w, s) for w, s in parts if s is not None]
    total = sum(w for w, _ in scored)
    return sum(w * s for w, s in scored) / total if total else None


def completion_score(gates: Sequence[GateResult], criteria: Sequence[CriterionScore]) -> float:
    """0 if a gate failed, else the weighted mean of the scored criteria (1 when every gate holds
    and nothing is scored, since nothing was found wrong)."""
    if any(g.passed is False for g in gates):
        return 0.0
    mean = weighted_mean([(c.weight, c.score) for c in criteria])
    return 1.0 if mean is None else _clamp(mean)


def combine(
    criteria: Sequence[Criterion],
    evidence: Mapping[str, Evidence],
    judged: Mapping[str, float] | None = None,
) -> list[CriterionScore]:
    """Each criterion's score: the judge's when it gave one, else the measurement."""
    out: list[CriterionScore] = []
    for c in criteria:
        measured = measured_score(c, evidence)
        said = (judged or {}).get(c.id)
        score = said if said is not None else measured
        basis = "judge" if said is not None else "measured" if measured is not None else "unscored"
        out.append(
            CriterionScore(
                id=c.id, weight=c.weight, score=score, basis=basis, measured=measured, judged=said
            )
        )
    return out
