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


def test_claims_json_has_no_markdown_layout_to_check() -> None:
    content = '{"summary": "ok", "claims": [{"text": "Missing guard", "kind": "finding"}]}'
    passed, issues = run_deterministic_checks(content, structured=True)
    assert passed and issues == []
    passed, issues = run_deterministic_checks(content)  # prose is still held to a layout
    assert not passed and any("structural" in i for i in issues)


def test_structured_answers_are_checked_for_cited_files_not_scraped_text() -> None:
    content = '{"summary": "see other/place.py in passing", "claims": []}'
    passed, _ = run_deterministic_checks(
        content, structured=True, known_files=["a.py"], claim_files=["a.py"]
    )
    assert passed  # the path in the summary is not a claim
    passed, issues = run_deterministic_checks(
        content, structured=True, known_files=["a.py"], claim_files=["src/b.py"]
    )
    assert not passed and any("src/b.py" in i for i in issues)


def test_claim_test_check_applies_to_coding_answers_only() -> None:
    from fusion.evals.deterministic import check_claims_include_tests

    assert check_claims_include_tests(["finding"], is_coding_task=True)[0] is False
    assert check_claims_include_tests(["finding", "test"], is_coding_task=True)[0] is True
    assert check_claims_include_tests(["finding"], is_coding_task=False)[0] is True
