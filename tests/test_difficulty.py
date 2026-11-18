"""Difficulty estimate: deterministic signals first, then a classifier call if unclear."""

from __future__ import annotations

import pytest

from fusion.config.loader import DifficultyConfig, load_routing_policies
from fusion.routing.classifier import TaskType
from fusion.routing.difficulty import (
    DifficultyEstimate,
    RequestFacts,
    build_difficulty_prompt,
    estimate_difficulty,
    parse_difficulty_reply,
)

SIGNALS = {
    "task",
    "size",
    "files",
    "hunks",
    "languages",
    "stack_depth",
    "error_type",
    "risk",
    "question",
}

_HUNK = "@@ -10,6 +10,9 @@ def handler(request):\n-    old()\n+    new()\n+    more()\n"


def _diff(files: list[str], hunks: int = 2) -> str:
    parts = []
    for name in files:
        parts.append(f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n" + _HUNK * hunks)
    return "\n".join(parts)


def test_a_short_yes_no_question_is_easy_and_clear() -> None:
    estimate = estimate_difficulty(
        "Is `len([])` equal to 0 in Python?", RequestFacts(task_type=TaskType.DEFAULT)
    )
    assert isinstance(estimate, DifficultyEstimate)
    assert estimate.level == "easy"
    assert not estimate.ambiguous
    assert set(estimate.signals) == SIGNALS
    assert estimate.signals["question"] == 0.0


def test_a_large_multi_language_diff_touching_auth_and_payments_is_hard() -> None:
    files = ["api/auth.py", "api/payments.py", "web/login.ts", "web/cart.ts", "db/0042.sql"]
    diff = _diff(files, hunks=4) + "\n# session token, password hash, refund amount, migration\n"
    estimate = estimate_difficulty(
        diff, RequestFacts(task_type=TaskType.CODE_REVIEW, changed_files=files)
    )
    assert estimate.level == "hard"
    assert estimate.signals["files"] == 1.0
    assert estimate.signals["languages"] > 0.5
    assert estimate.signals["risk"] == 1.0


def test_a_deep_trace_from_a_race_scores_higher_than_a_shallow_key_error() -> None:
    shallow = (
        'Traceback (most recent call last):\n  File "app.py", line 3, in <module>\n'
        "    x = d['k']\nKeyError: 'k'"
    )
    frames = "".join(
        f'  File "pool/worker{i}.py", line {i}, in run\n    step()\n' for i in range(20)
    )
    deep = (
        f"Intermittent deadlock under load, threads hang:\nTraceback (most recent call last):\n"
        f"{frames}RuntimeError: lock acquired twice"
    )
    facts = RequestFacts(task_type=TaskType.DEBUGGING)
    easy, hard = estimate_difficulty(shallow, facts), estimate_difficulty(deep, facts)
    assert easy.signals["stack_depth"] < hard.signals["stack_depth"] == 1.0
    assert easy.signals["error_type"] < hard.signals["error_type"]
    assert easy.score < hard.score
    assert easy.level != "hard"


def test_a_design_question_scores_higher_than_a_lookup() -> None:
    facts = RequestFacts(task_type=TaskType.DEFAULT)
    lookup = estimate_difficulty("What does `str.strip` return?", facts)
    design = estimate_difficulty(
        "Should we split the billing service out, and what are the trade-offs of that design?",
        facts,
    )
    assert lookup.signals["question"] < design.signals["question"] == 1.0
    assert lookup.score < design.score


def test_a_score_close_to_a_threshold_is_ambiguous_with_low_confidence() -> None:
    config = DifficultyConfig()
    facts = RequestFacts(task_type=TaskType.DEFAULT)
    clear = estimate_difficulty("Is 2 > 1?", facts, config)
    near = config.model_copy(update={"easy_below": clear.score + 0.01})
    unclear = estimate_difficulty("Is 2 > 1?", facts, near)
    assert clear.confidence >= 0.5 and not clear.ambiguous
    assert unclear.ambiguous and unclear.confidence < 0.5


def test_thresholds_and_weights_come_from_routing_policies() -> None:
    config = load_routing_policies().routing.difficulty
    assert 0 < config.easy_below < config.hard_from < 1
    assert set(config.weights) == SIGNALS
    assert sum(config.weights.values()) == pytest.approx(1.0)
    assert config.classifier_max_tokens <= 150


def test_the_classifier_prompt_and_reply() -> None:
    prompt = build_difficulty_prompt("Why is my cache stale?", TaskType.DEBUGGING)
    assert "debugging" in prompt and "easy" in prompt and "hard" in prompt
    assert parse_difficulty_reply('{"level": "hard", "why": "many files"}') == "hard"
    assert parse_difficulty_reply('noise {"level": "Normal"} noise') == "normal"
    assert parse_difficulty_reply('{"level": "trivial"}') is None
    assert parse_difficulty_reply("not json") is None
