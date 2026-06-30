"""Tests for deterministic evaluations."""

from fusion.evals.deterministic import (
    check_no_secret_leakage,
    check_response_completeness,
    run_deterministic_checks,
)


def test_completeness_passes() -> None:
    content = "A" * 100
    ok, issues = check_response_completeness(content)
    assert ok
    assert issues == []


def test_completeness_fails_short() -> None:
    ok, issues = check_response_completeness("short")
    assert not ok
    assert len(issues) >= 1


def test_secret_leakage_detected() -> None:
    ok, issues = check_no_secret_leakage("key=AKIAIOSFODNN7EXAMPLE")
    assert not ok
    assert len(issues) >= 1


def test_deterministic_checks_panel_response() -> None:
    content = "## Review\n\n**Finding:** Missing error handling.\n" + "Detail. " * 20
    passed, issues = run_deterministic_checks(content)
    assert passed
    assert issues == []


def test_deterministic_checks_judge_json() -> None:
    content = '{"specificity": 0.8, "overall_score": 0.75}'
    passed, _ = run_deterministic_checks(content, is_judge=True, min_length=10)
    assert passed
