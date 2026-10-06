"""Gates, measured criteria and the completion score: from evidence to a score."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from _agentic import evidence
from fusion.bench.scoring.completion import (
    CriterionScore,
    GateResult,
    by_kind,
    combine,
    completion_score,
    criteria_for,
    default_criteria,
    default_gates,
    evaluate_gate,
    gates_for,
    measured_score,
    weighted_mean,
)
from fusion.bench.spec import BenchTask, Criterion, Gate


def task(category: str = "frontend", **truth: object) -> BenchTask:
    return BenchTask.model_validate(
        {"id": "t", "category": category, "prompt": "Build it.", "truth": dict(truth)}
    )


def crit(source: str, weight: float = 1.0, id: str | None = None) -> Criterion:
    return Criterion(id=id or source, weight=weight, source=source)  # type: ignore[arg-type]


# -- gates ------------------------------------------------------------------------------------


def test_a_gate_without_a_metric_follows_the_evidence_verdict() -> None:
    gate = Gate(id="tests-pass", evidence="tests")
    assert evaluate_gate(gate, by_kind([evidence("tests", True)])).passed is True
    failed = evaluate_gate(gate, by_kind([evidence("tests", False)]))
    assert failed.passed is False and "hidden tests fail" in failed.detail


def test_a_gate_with_a_metric_checks_its_bounds() -> None:
    gate = Gate(id="small", evidence="static", metric="lines", max=10, min=2)
    for value, passed in ((5, True), (10, True), (11, False), (1, False)):
        found = evidence("static", True, metrics={"lines": float(value)})
        assert evaluate_gate(gate, by_kind([found])).passed is passed


def test_evidence_that_could_not_be_taken_leaves_the_gate_unverified_not_failed() -> None:
    gate = Gate(id="g", evidence="screenshot")
    assert evaluate_gate(gate, {}).passed is None
    skipped = evidence("screenshot", None, status="skipped", summary="no browser installed")
    result = evaluate_gate(gate, by_kind([skipped]))
    assert result.passed is None and "no browser installed" in result.detail
    noisy = evidence("perf", None, status="unstable", summary="too noisy")
    assert evaluate_gate(Gate(id="p", evidence="perf"), by_kind([noisy])).passed is None
    unknown = evaluate_gate(
        Gate(id="m", evidence="tests", metric="nope", max=1), by_kind([evidence()])
    )
    assert unknown.passed is None


def test_a_gate_with_bounds_needs_a_metric_and_the_reverse() -> None:
    with pytest.raises(ValidationError):
        Gate(id="x", evidence="tests", max=3)
    with pytest.raises(ValidationError):
        Gate(id="x", evidence="tests", metric="m")


# -- completion -------------------------------------------------------------------------------


def scored(*pairs: tuple[float, float | None]) -> list[CriterionScore]:
    return [
        CriterionScore(id=f"c{i}", weight=w, score=s, basis="measured")
        for i, (w, s) in enumerate(pairs)
    ]


def test_a_failed_hard_gate_completes_nothing_however_good_the_rest() -> None:
    gates = [GateResult(id="a", passed=True), GateResult(id="b", passed=False)]
    assert completion_score(gates, scored((1, 1.0), (1, 1.0))) == 0.0


def test_unverified_gates_do_not_fail_an_answer() -> None:
    gates = [GateResult(id="a", passed=None), GateResult(id="b", passed=True)]
    assert completion_score(gates, scored((1, 0.5))) == pytest.approx(0.5)


def test_the_score_is_the_weighted_mean_of_the_scored_criteria() -> None:
    parts = scored((3, 1.0), (1, 0.0), (2, None))  # the unscored one is left out, not counted as 0
    assert completion_score([], parts) == pytest.approx(0.75)
    assert weighted_mean([(2.0, 1.0), (2.0, None)]) == 1.0
    assert weighted_mean([(1.0, None)]) is None


def test_nothing_scored_and_no_gate_failed_is_a_full_score() -> None:
    assert completion_score([GateResult(id="a", passed=True)], scored((1, None))) == 1.0


# -- measured criteria --------------------------------------------------------------------------


def test_tests_are_scored_by_their_pass_fraction() -> None:
    found = evidence("tests", False, metrics={"pass_fraction": 0.25})
    assert measured_score(crit("tests"), by_kind([found])) == 0.25
    assert measured_score(crit("tests"), {}) is None


def test_static_findings_cost_points() -> None:
    clean = evidence("static", True, metrics={"lint_issues": 0.0})
    messy = evidence(
        "static",
        False,
        metrics={
            "lint_issues": 3.0,
            "max_complexity": 15.0,
            "external_requests": 1.0,
            "new_dependencies": 1.0,
        },
    )
    assert measured_score(crit("static"), by_kind([clean])) == 1.0
    # 3 lint findings (0.3), complexity 5 over the limit (0.15), a new dependency (0.15), a request
    # to another host (0.2).
    assert measured_score(crit("static"), by_kind([messy])) == pytest.approx(0.2)
    mild = evidence("static", False, metrics={"lint_issues": 2.0})
    assert measured_score(crit("static"), by_kind([mild])) == pytest.approx(0.8)


def test_accessibility_weights_violations_by_impact() -> None:
    browser = evidence("a11y", False, metrics={"critical": 1.0, "serious": 1.0, "engine": 1.0})
    assert measured_score(crit("a11y"), by_kind([browser])) == pytest.approx(1 - 1.5 / 3)
    # The source-only engine sees less, so a clean result is not full credit.
    static = evidence("a11y", True, metrics={"engine": 0.0})
    assert measured_score(crit("a11y"), by_kind([static])) == pytest.approx(0.9)


def test_performance_score_falls_with_the_ratio_and_the_scaling_excess() -> None:
    def score(ratio: float, excess: float = 0.0, status: str = "measured") -> float | None:
        item = evidence(
            "perf",
            None,
            metrics={"ratio_vs_reference": ratio, "scaling_excess": excess},
            status=status,
        )
        return measured_score(crit("perf"), by_kind([item]))

    assert score(1.0) == 1.0 and score(1.25) == 1.0
    assert score(10.0) == pytest.approx(0.0)
    middle = score(3.5)
    assert middle is not None and 0.0 < middle < 1.0
    assert score(1.0, excess=1.25) == pytest.approx(0.0)  # fast but scales badly
    assert score(1.0, status="unstable") is None  # a noisy measurement is not scored


def test_the_visual_score_counts_the_pages_basic_checks() -> None:
    shot = evidence(
        "screenshot", True, metrics={"loads": 1.0, "text_chars": 120.0, "overflow_x_mobile_px": 0.0}
    )
    console = evidence("console", False, metrics={"console_errors": 2.0, "failed_requests": 0.0})
    assert measured_score(crit("visual"), by_kind([shot])) == 1.0
    assert measured_score(crit("visual"), by_kind([shot, console])) == pytest.approx(4 / 5)
    wide = evidence(
        "screenshot",
        False,
        metrics={"loads": 1.0, "text_chars": 120.0, "overflow_x_mobile_px": 80.0},
    )
    assert measured_score(crit("visual"), by_kind([wide])) == pytest.approx(2 / 3)
    skipped = evidence("screenshot", None, status="skipped")
    assert measured_score(crit("visual"), by_kind([skipped])) is None


def test_a_judge_only_criterion_has_no_measurement() -> None:
    assert measured_score(crit("judge"), by_kind([evidence()])) is None


def test_the_judges_score_wins_over_the_measurement_and_basis_says_which() -> None:
    items = by_kind([evidence("tests", True, metrics={"pass_fraction": 1.0})])
    criteria = [
        crit("tests", 3, "correctness"),
        crit("judge", 1, "design"),
        crit("a11y", 1, "access"),
    ]
    rows = {c.id: c for c in combine(criteria, items, {"correctness": 0.6, "design": 0.8})}
    assert (rows["correctness"].score, rows["correctness"].basis) == (0.6, "judge")
    assert rows["correctness"].measured == 1.0 and rows["correctness"].judged == 0.6
    assert (rows["design"].score, rows["design"].basis) == (0.8, "judge")
    assert (rows["access"].score, rows["access"].basis) == (None, "unscored")
    plain = {c.id: c.basis for c in combine(criteria, items)}
    assert plain == {"correctness": "measured", "design": "unscored", "access": "unscored"}


# -- defaults ------------------------------------------------------------------------------------


def test_a_task_that_names_no_gates_or_criteria_gets_its_categorys_defaults() -> None:
    perf, page = task("performance"), task("frontend")
    assert [g.id for g in gates_for(perf)] == ["tests-pass", "fast-enough"]
    assert [g.id for g in gates_for(page)] == ["tests-pass"]
    assert {c.source for c in criteria_for(perf)} == {"tests", "perf", "static"}
    assert {"a11y", "visual", "judge"} <= {c.source for c in criteria_for(page)}
    assert default_gates(perf)[1].evidence == "perf"
    assert sum(c.weight for c in default_criteria(page)) > 0


def test_a_task_overrides_the_defaults_with_its_own() -> None:
    mine = task(
        "frontend",
        hard_gates=[{"id": "only", "evidence": "console"}],
        soft_criteria=[{"id": "look", "weight": 2, "source": "judge"}],
    )
    assert [g.id for g in gates_for(mine)] == ["only"]
    assert [c.id for c in criteria_for(mine)] == ["look"]


def test_gate_and_criterion_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="unique"):
        task(
            "frontend",
            hard_gates=[{"id": "x", "evidence": "tests"}],
            soft_criteria=[{"id": "x", "source": "tests"}],
        )
