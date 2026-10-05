"""DebugScorer (root-cause rank and fix) and RubricScorer (checklist with a quoting judge)."""

from __future__ import annotations

import pytest

from _judge import CALL_COST, make_task, score_env
from fusion.bench.scoring import (
    AnswerView,
    DebugScorer,
    Evidence,
    EvidenceItem,
    RubricScorer,
    ScoringError,
)
from fusion.bench.scoring.base import segments
from fusion.bench.scoring.debug import rank_credit
from fusion.bench.spec import BenchTask, RubricTruth

DEBUG_TRUTH = {
    "root_cause_tags": ["race-condition", "data-race"],
    "root_cause_aliases": {"race-condition": ["two threads write the counter"]},
    "root_cause": "two threads update the shared counter without a lock",
    "fix_keywords": ["lock|mutex", "atomic"],
}


def debug_task() -> BenchTask:
    return make_task("debugging", DEBUG_TRUTH)


async def debug_score(text: str, replies=None):
    env, provider = score_env(replies)
    result = await DebugScorer().score(debug_task(), AnswerView(final_answer=text), env)
    return result, provider


# -- segments ------------------------------------------------------------------------------------


def test_segments_are_the_list_entries_when_there_is_a_list() -> None:
    text = "Here is my analysis of the failure.\n1. The cache is stale\n2. A race condition here\n"
    assert segments(text) == ["The cache is stale", "A race condition here"]


def test_segments_are_paragraphs_without_a_list_and_drop_labels() -> None:
    text = "## Cause\n\nThe handler mutates shared state\nfrom two threads.\n\nFix it with a lock."
    assert segments(text) == [
        "The handler mutates shared state from two threads.",
        "Fix it with a lock.",
    ]


# -- debug ---------------------------------------------------------------------------------------


def test_rank_credit() -> None:
    assert [rank_credit(r) for r in (1, 2, 3, 4, None)] == [1.0, 0.7, 0.7, 0.3, 0.0]


async def test_the_cause_first_with_the_whole_fix_is_perfect() -> None:
    result, _ = await debug_score(
        "1. A race condition on the shared counter\n2. The cache is stale\n"
        "Fix: guard it with a mutex, or use an atomic increment."
    )
    assert result.quality == pytest.approx(1.0)
    assert result.details["top1"] is True
    assert result.details["method"] == "tag"
    assert result.details["fix_missed"] == []


async def test_the_cause_in_second_place_earns_seventy_percent() -> None:
    result, _ = await debug_score(
        "1. The network is flaky\n2. This is a data race on the counter\n3. Stale cache\n"
        "Use a lock and an atomic add."
    )
    assert result.details["rank"] == 2
    assert result.details["top1"] is False
    assert result.details["top3"] is True
    assert result.quality == pytest.approx(0.7 * 0.7 + 0.3)


async def test_a_late_mention_earns_thirty_percent_of_the_cause() -> None:
    items = "\n".join(f"{n}. unrelated guess number {n} here" for n in range(1, 5))
    result, _ = await debug_score(f"{items}\n5. A race-condition perhaps")
    assert result.details["rank"] == 5
    assert result.details["cause_credit"] == 0.3
    assert result.details["top3"] is False


async def test_an_alias_names_the_cause() -> None:
    result, _ = await debug_score("1. Two threads write the counter at once\n2. cache stale")
    assert result.details["rank"] == 1


async def test_a_missed_cause_scores_only_the_fix() -> None:
    result, _ = await debug_score("1. The disk is full\n2. Add a mutex around it somewhere")
    assert result.details["rank"] is None
    assert result.quality == pytest.approx(0.3 * 0.5)  # the lock|mutex element, not atomic


async def test_without_fix_keywords_the_cause_alone_decides() -> None:
    truth = {"root_cause_tags": ["off-by-one"]}
    env, _ = score_env()
    task = make_task("debugging", truth)
    result = await DebugScorer().score(
        task, AnswerView(final_answer="1. An off by one in the loop bound"), env
    )
    assert result.quality == 1.0  # "off by one" matches "off-by-one" once separators are equal


async def test_a_judge_decides_equivalence_only_when_no_tag_matched() -> None:
    def judge(kind: str, payload: dict) -> dict | None:
        assert kind == "equivalence"
        assert payload["cause"] == DEBUG_TRUTH["root_cause"]
        return {"equivalent": [2]}

    result, provider = await debug_score(
        "1. The disk is full\n2. Concurrent updates to the counter lack mutual exclusion\n",
        replies={"claude-haiku": judge},
    )
    assert result.details["method"] == "judge"
    assert result.details["rank"] == 2
    assert result.cost_usd == pytest.approx(CALL_COST)
    assert len(provider.payloads("equivalence")) == 1

    _, provider = await debug_score(
        "1. A race condition\n2. other things to try here", replies={"claude-haiku": judge}
    )
    assert provider.requests == []  # the tag matched: no judge needed


async def test_the_judge_only_sees_the_first_five_hypotheses() -> None:
    seen: list[dict] = []

    def judge(kind: str, payload: dict) -> dict | None:
        seen.append(payload)
        return {"equivalent": [6]}  # an id it was never shown is ignored

    items = "\n".join(f"{n}. guess number {n} about the failure" for n in range(1, 9))
    result, _ = await debug_score(items, replies={"claude-haiku": judge})
    assert len(seen[0]["candidates"]) == 5
    assert result.details["rank"] is None


async def test_debug_falls_back_to_points() -> None:
    task = make_task("debugging", {"points": [{"id": "p", "keywords": ["mutex"]}]})
    env, _ = score_env()
    result = await DebugScorer().score(task, AnswerView(final_answer="use a mutex"), env)
    assert result.scorer == "points"


# -- rubric --------------------------------------------------------------------------------------

RUBRIC = {
    "required_points": [
        {"id": "r1", "text": "Name the failure mode of the queue", "keywords": ["backpressure"]},
        {"id": "r2", "text": "Say how retries are bounded", "keywords": ["max retries", "backoff"]},
        {"id": "r3", "text": "Choose one datastore", "keywords": ["postgres"], "gate": True},
    ],
    "forbidden_points": [{"id": "f1", "text": "Recommend a rewrite", "keywords": ["rewrite"]}],
}


def rubric_task(truth: dict | None = None) -> BenchTask:
    return make_task("architecture", truth or RUBRIC)


async def rubric_score(text: str, replies=None, truth=None, evidence=None):
    env, provider = score_env(replies)
    result = await RubricScorer().score(
        rubric_task(truth), AnswerView(final_answer=text), env, evidence
    )
    return result, provider


async def test_keyword_items_are_decided_without_a_judge() -> None:
    result, provider = await rubric_score("Use Postgres, add backpressure and exponential backoff.")
    assert result.quality == pytest.approx(1.0)
    assert set(result.details["decided_by"].values()) == {"keywords"}
    assert provider.requests == []
    assert result.cost_usd == 0.0


async def test_a_missed_gate_caps_quality_below_the_pass_mark() -> None:
    result, _ = await rubric_score("Add backpressure and a backoff between retries.")
    assert result.details["fraction"] == pytest.approx(2 / 3)
    assert result.details["gates_missed"] == ["r3"]
    assert result.quality == 0.4


async def test_a_forbidden_point_is_penalised() -> None:
    result, _ = await rubric_score("Use postgres, backpressure, backoff. Or just rewrite it.")
    assert result.details["forbidden_asserted"] == ["f1"]
    assert result.quality == pytest.approx(0.5)


async def test_items_without_keywords_and_no_judge_cannot_be_decided() -> None:
    truth = {"required_points": ["Name the failure mode", "Say how retries are bounded"]}
    with pytest.raises(ScoringError, match="--judge-models"):
        await rubric_score("anything at all", truth=truth)


async def test_plain_string_items_get_ids() -> None:
    task = rubric_task({"required_points": ["first thing", "second thing"]})
    parsed = RubricTruth.model_validate(task.truth)
    assert [i.id for i in parsed.required_points] == ["r1", "r2"]


def met(quote: str, *ids: str, unmet: tuple[str, ...] = ()):
    rows = [{"id": i, "met": True, "quote": quote} for i in ids]
    rows += [{"id": i, "met": False, "quote": ""} for i in unmet]
    return lambda kind, payload: {"items": rows}


TEXT = "We use Postgres for storage. Producers see backpressure when the queue is full."


async def test_a_judged_item_needs_a_quote_that_is_in_the_answer() -> None:
    truth = {
        "required_points": [
            {"id": "r1", "text": "Choose one datastore"},
            {"id": "r2", "text": "Describe queue overload behaviour"},
        ]
    }
    judge = lambda k, p: {  # noqa: E731
        "items": [
            {"id": "r1", "met": True, "quote": "We use Postgres for storage."},
            {"id": "r2", "met": True, "quote": "a sentence the answer never wrote"},
        ]
    }
    result, _ = await rubric_score(TEXT, replies={"claude-haiku": judge}, truth=truth)
    assert result.details["met"] == ["r1"]
    assert result.details["missed"] == ["r2"]
    assert result.quality == pytest.approx(0.5)
    assert result.cost_usd == pytest.approx(CALL_COST)


async def test_several_judges_decide_each_item_by_majority() -> None:
    truth = {"required_points": [{"id": "r1", "text": "Choose one datastore"}]}
    yes = met("We use Postgres for storage.", "r1")
    no = met("", unmet=("r1",))
    result, provider = await rubric_score(
        TEXT,
        replies={"claude-haiku": yes, "gemini-flash": yes, "gpt-luna": no},
        truth=truth,
    )
    assert result.details["met"] == ["r1"]
    assert provider.kinds() == ["rubric"] * 3
    result, _ = await rubric_score(
        TEXT, replies={"claude-haiku": yes, "gemini-flash": no, "gpt-luna": no}, truth=truth
    )
    assert result.details["met"] == []


async def test_a_judge_failure_falls_back_to_keywords_where_there_are_some() -> None:
    result, _ = await rubric_score(
        "Use postgres. Add backpressure. Use backoff.", replies={"claude-haiku": lambda k, p: None}
    )
    assert result.quality == 1.0
    assert "judge_errors" in result.details


async def test_evidence_is_shown_to_the_judge_and_may_be_quoted() -> None:
    truth = {"required_points": [{"id": "r1", "text": "The change passes its tests"}]}
    evidence = Evidence(items=[EvidenceItem(kind="test", name="suite", passed=True, value=12)])
    judge = met("[test] suite = 12 PASS", "r1")
    result, provider = await rubric_score(
        "The patch is applied.", replies={"claude-haiku": judge}, truth=truth, evidence=evidence
    )
    assert "[test] suite = 12 PASS" in provider.requests[0].user_prompt
    assert result.quality == 1.0


async def test_rubric_falls_back_to_points() -> None:
    task = make_task("planning", {"points": [{"id": "p", "keywords": ["milestone"]}]})
    env, _ = score_env()
    result = await RubricScorer().score(task, AnswerView(final_answer="a milestone plan"), env)
    assert result.scorer == "points"


def test_rubric_ids_must_be_unique() -> None:
    with pytest.raises(ValueError, match="unique"):
        rubric_task(
            {
                "required_points": [{"id": "x", "text": "a"}],
                "forbidden_points": [{"id": "x", "text": "b"}],
            }
        )


def test_the_planner_prices_one_call_per_judge() -> None:
    calls = RubricScorer().estimate_calls(rubric_task(), ["claude-haiku", "gpt-luna"])
    assert [c.alias for c in calls] == ["claude-haiku", "gpt-luna"]
    assert RubricScorer().estimate_calls(rubric_task(), []) == []
