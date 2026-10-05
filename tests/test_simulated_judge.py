"""The simulated judge: decides by word overlap, errs in proportion to 1 - skill, repeatably."""

from __future__ import annotations

import json
from typing import Any

from _judge import make_task
from fusion.providers.simulated import SimModel, SimWorld
from fusion.providers.simulated_judge import judge_reply

WORLD = SimWorld([make_task("architecture", {"required_points": ["x"]})])
SURE = SimModel(skill=1.0)  # never slips
SLOPPY = SimModel(skill=0.0)


def reply(
    kind: str, payload: dict[str, Any], spec: SimModel = SURE, seed: int = 0
) -> dict[str, Any]:
    raw = judge_reply(
        {"kind": kind, "payload": payload}, model="m", spec=spec, world=WORLD, seed=seed
    )
    assert raw is not None
    return json.loads(raw)


def test_match_pairs_each_defect_with_the_finding_that_describes_it() -> None:
    payload = {
        "bugs": [
            {"id": "b1", "description": "amount rounding drops cents"},
            {"id": "b2", "description": "token never expires"},
        ],
        "findings": [
            {"id": 0, "text": "The rounding of the amount silently drops cents"},
            {"id": 1, "text": "Consider renaming this helper"},
        ],
    }
    rows = {m["defect"]: m["finding"] for m in reply("match", payload)["matches"]}
    assert rows == {"b1": 0, "b2": None}


def test_equivalence_picks_the_hypotheses_that_say_the_cause() -> None:
    payload = {
        "cause": "two threads update the shared counter",
        "candidates": [
            {"id": 1, "text": "the disk is full"},
            {"id": 2, "text": "two threads update that shared counter concurrently"},
        ],
    }
    assert reply("equivalence", payload)["equivalent"] == [2]


def test_rubric_quotes_the_sentence_that_meets_an_item() -> None:
    payload = {
        "answer": "We store orders in postgres. Retries are capped at five attempts.",
        "items": [
            {"id": "r1", "text": "store orders in postgres"},
            {"id": "r2", "text": "shard by tenant"},
        ],
    }
    rows = {r["id"]: r for r in reply("rubric", payload)["items"]}
    assert rows["r1"]["met"] is True
    assert rows["r1"]["quote"] in payload["answer"]
    assert rows["r2"]["met"] is False


def test_a_sloppy_judge_slips_more_often_than_a_sure_one_and_repeatably() -> None:
    payload = {
        "cause": "two threads update the shared counter",
        "candidates": [{"id": n, "text": "unrelated words appear here"} for n in range(60)],
    }
    sure = reply("equivalence", payload, SURE)["equivalent"]
    sloppy = reply("equivalence", payload, SLOPPY)["equivalent"]
    assert sure == []
    assert sloppy  # some false positives
    assert sloppy == reply("equivalence", payload, SLOPPY)["equivalent"]


def test_a_call_it_does_not_understand_returns_none() -> None:
    assert judge_reply(None, model="m", spec=SURE, world=WORLD, seed=0) is None
    assert (
        judge_reply({"kind": "other", "payload": {}}, model="m", spec=SURE, world=WORLD, seed=0)
        is None
    )
