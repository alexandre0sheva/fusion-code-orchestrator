"""ReviewScorer: seeded-bug recall and precision, deterministic first, a judge for the leftovers."""

from __future__ import annotations

import pytest

from _judge import CALL_COST, make_task, score_env
from fusion.bench.scoring import AnswerView, ReviewScorer, ScoringError
from fusion.bench.scoring.review import extract_findings

FILES = {
    "app/db.py": "\n".join(f"db line {n}" for n in range(1, 101)),
    "app/views.py": "\n".join(f"view line {n}" for n in range(1, 101)),
}
BUGS = [
    {
        "file": "app/db.py",
        "line": 12,
        "category": "sql-injection",
        "severity": "high",
        "description": "query built by string formatting",
        "aliases": ["sql injection"],
    },
    {
        "file": "app/views.py",
        "line": 20,
        "category": "missing-auth",
        "severity": "medium",
        "description": "delete view has no permission check",
    },
]


def review_task(bugs: list[dict] | None = None, files: dict[str, str] | None = None):
    return make_task("code_review", {"bugs": BUGS if bugs is None else bugs}, files=files or FILES)


async def score(task, text, **env_args):
    env, provider = score_env(env_args.get("replies"))
    result = await ReviewScorer().score(task, AnswerView(final_answer=text), env)
    return result, provider


async def test_finds_both_bugs_at_their_lines_with_their_categories() -> None:
    result, _ = await score(
        review_task(),
        "- app/db.py:12 SQL injection: the query is built with an f-string\n"
        "- app/views.py:21 missing auth on the delete view",
    )
    assert result.quality == pytest.approx(1.0)
    assert result.details["recall"] == 1.0
    assert result.details["precision"] == 1.0
    assert [m["how"] for m in result.details["matched"]] == ["location+category"] * 2
    assert result.cost_usd == 0.0


async def test_a_missed_bug_costs_recall_weighted_by_severity() -> None:
    result, _ = await score(review_task(), "- app/db.py:12 SQL injection in the query")
    # high = 2, medium = 1: recall 2/3, precision 1, F1 0.8
    assert result.details["recall"] == pytest.approx(2 / 3)
    assert result.quality == pytest.approx(0.8)
    assert result.details["missed"] == [2]


async def test_a_false_positive_costs_precision() -> None:
    result, _ = await score(
        review_task(),
        "- app/db.py:12 SQL injection here\n- app/views.py:20 missing auth check\n"
        "- app/db.py:90 this import is unused",
    )
    assert result.details["precision"] == pytest.approx(2 / 3)
    assert result.details["false_positives"] == [
        {"file": "app/db.py", "line": 90, "hallucinated": False}
    ]


async def test_a_hallucinated_file_counts_double_and_is_named() -> None:
    result, _ = await score(
        review_task(),
        "- app/db.py:12 SQL injection here\n- app/views.py:20 missing auth check\n"
        "- app/ghost.py:5 a race condition",
    )
    assert result.details["precision"] == pytest.approx(2 / 4)  # two hits, a false positive of 2
    assert result.details["hallucinated_files"] == ["app/ghost.py"]


async def test_a_finding_at_the_right_place_that_names_no_category_gets_half_credit() -> None:
    result, _ = await score(
        review_task(BUGS[:1]), "- app/db.py:12 this line is suspicious and should change"
    )
    assert result.details["matched"][0]["how"] == "location"
    assert result.details["recall"] == pytest.approx(0.5)


async def test_line_tolerance_is_three_lines_either_side() -> None:
    near, _ = await score(review_task(BUGS[:1]), "- app/db.py:15 SQL injection")
    far, _ = await score(review_task(BUGS[:1]), "- app/db.py:16 SQL injection")
    assert near.details["recall"] == 1.0
    assert far.details["recall"] == 0.0
    assert far.details["false_positives"]


async def test_a_line_range_overlapping_the_tolerance_matches() -> None:
    result, _ = await score(review_task(BUGS[:1]), "- app/db.py:5-10 SQL injection")
    assert result.details["recall"] == 1.0


async def test_alternative_ways_to_cite_a_location_are_read() -> None:
    text = (
        "1. In app/db.py, line 12 there is a sql injection\n"
        "2. app/views.py (line 20) is missing auth\n"
    )
    assert [(f.file, f.line) for f in extract_findings(text, list(FILES))] == [
        ("app/db.py", 12),
        ("app/views.py", 20),
    ]


async def test_two_findings_at_one_bug_are_not_a_false_positive() -> None:
    result, _ = await score(
        review_task(BUGS[:1]),
        "- app/db.py:12 SQL injection in the query\n- app/db.py:13 the same SQL injection again",
    )
    assert result.details["duplicates"] == 1
    assert result.details["false_positives"] == []
    assert result.quality == pytest.approx(1.0)


async def test_a_bare_line_number_is_taken_in_a_single_file_task() -> None:
    files = {"only.py": "\n".join(str(n) for n in range(50))}
    bug = {**BUGS[0], "file": "only.py"}
    result, _ = await score(review_task([bug], files), "- SQL injection at line 12")
    assert result.details["recall"] == 1.0


async def test_paths_with_diff_prefixes_resolve() -> None:
    result, _ = await score(review_task(BUGS[:1]), "- b/app/db.py:12 SQL injection")
    assert result.details["recall"] == 1.0
    assert result.details["false_positives"] == []


async def test_a_clean_change_with_no_findings_is_perfect() -> None:
    result, _ = await score(review_task([]), "Looks good. No issues found.")
    assert result.quality == 1.0
    assert result.details["clean_task"] is True


async def test_one_false_alarm_on_clean_code_is_not_solved() -> None:
    result, _ = await score(review_task([]), "- app/db.py:12 possible SQL injection")
    assert result.quality == pytest.approx(0.5)
    assert result.quality < 0.6


async def test_an_unlocated_remark_on_clean_code_is_not_penalised() -> None:
    result, _ = await score(review_task([]), "- Consider adding a docstring to the module")
    assert result.quality == 1.0
    assert result.details["unlocated_findings"] == 1


async def test_an_answer_that_finds_nothing_scores_zero_when_there_are_bugs() -> None:
    result, _ = await score(review_task(), "Looks fine to me, ship it.")
    assert result.quality == 0.0


async def test_a_judge_matches_only_what_the_locations_missed() -> None:
    def judge(kind: str, payload: dict) -> dict | None:
        assert kind == "match"
        (offered,) = payload["findings"]  # the db.py finding was matched by location: not offered
        return {"matches": [{"defect": "b2", "finding": offered["id"], "quote": "no check"}]}

    text = (
        "- app/db.py:12 SQL injection in the query\n"
        "- the delete endpoint has no permission check for anonymous users"
    )
    result, provider = await score(review_task(), text, replies={"claude-haiku": judge})
    assert result.details["recall"] == 1.0
    assert [m["how"] for m in result.details["matched"]] == ["location+category", "judge"]
    assert [b["id"] for b in provider.payloads("match")[0]["bugs"]] == ["b2"]  # only the leftover
    assert result.cost_usd == pytest.approx(CALL_COST)


async def test_the_judge_is_not_called_when_locations_found_everything() -> None:
    _, provider = await score(
        review_task(),
        "- app/db.py:12 SQL injection\n- app/views.py:20 missing auth",
        replies={"claude-haiku": lambda k, p: {"matches": []}},
    )
    assert provider.requests == []


async def test_a_judge_cannot_match_a_finding_it_was_not_shown() -> None:
    def judge(kind: str, payload: dict) -> dict | None:
        return {"matches": [{"defect": "b2", "finding": 99, "quote": "x"}]}

    result, _ = await score(
        review_task(BUGS[1:]), "- the code looks risky overall", replies={"claude-haiku": judge}
    )
    assert result.details["missed"] == [1]


async def test_a_failed_judge_leaves_the_bug_missed_and_says_why() -> None:
    result, _ = await score(
        review_task(BUGS[1:]),
        "- the delete endpoint skips the permission check",
        replies={"claude-haiku": lambda k, p: None},
    )
    assert result.details["missed"] == [1]
    assert "judge_errors" in result.details


async def test_a_task_in_points_format_is_scored_by_points() -> None:
    task = make_task("code_review", {"points": [{"id": "p", "keywords": ["alpha"]}]})
    result, _ = await score(task, "alpha is the problem")
    assert result.scorer == "points"
    assert result.quality == 1.0


async def test_a_task_with_no_usable_truth_is_an_error() -> None:
    with pytest.raises(ScoringError, match="cannot score task"):
        await score(make_task("code_review", {"other": 1}), "anything")


def test_the_planner_prices_one_judge_call_only_when_a_judge_is_named_and_bugs_exist() -> None:
    scorer = ReviewScorer()
    assert scorer.estimate_calls(review_task(), []) == []
    assert scorer.estimate_calls(review_task([]), ["claude-haiku"]) == []
    (call,) = scorer.estimate_calls(review_task(), ["claude-haiku"])
    assert call.alias == "claude-haiku"
    assert call.input_tokens > 0
