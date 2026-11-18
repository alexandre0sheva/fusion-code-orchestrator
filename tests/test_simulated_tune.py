"""Simulated models react to prompt edits (planted effects), and answer the tuner's roles."""

from __future__ import annotations

import json

import pytest

from fusion.bench.spec import BenchTask
from fusion.config.prompt_pack import PROMPT_SPECS
from fusion.providers.base import ModelRequest
from fusion.providers.simulated import SimModel, SimulatedProvider, SimWorld
from fusion.providers.simulated_tune import (
    SIM_HINTS,
    TUNE_COMPOSER_ROLE,
    TUNE_PROPOSER_ROLE,
    sim_prompt_effects,
)


def _task(n: int, category: str = "code_review") -> BenchTask:
    return BenchTask.model_validate(
        {
            "id": f"t{n}",
            "category": category,
            "difficulty": "medium",
            "prompt": f"Review change {n} of the ledger module.",
            "truth": {
                "points": [
                    {"id": f"p{n}{i}", "keywords": [f"kw{n}-{i}"]} for i in range(12)
                ],
                "decoys": [],
            },
        }
    )


def _found(provider: SimulatedProvider, task: BenchTask, system: str) -> int:
    request = ModelRequest(
        model_id="m",
        system_prompt=system,
        user_prompt=task.prompt,
        metadata={"role": "panel"},
        seed=1,
    )
    text = provider._answer(request, provider.models["m"], "panel", task.prompt, task)
    return len(json.loads(text)["claims"]) - 1  # the last claim is the canned test claim


def test_a_planted_marker_raises_skill_only_where_it_applies() -> None:
    tasks = [_task(1), _task(2, "debugging")]
    effects = {"MARKER": {"code_review": 0.3}}
    world = SimWorld(tasks, prompt_effects=effects)
    plain = SimWorld(tasks)
    sim = SimulatedProvider("p", {"m": SimModel(skill=0.4)}, world)
    base = SimulatedProvider("p", {"m": SimModel(skill=0.4)}, plain)
    assert _found(sim, tasks[0], "Be careful. MARKER") > _found(base, tasks[0], "Be careful.")
    assert _found(sim, tasks[0], "Be careful.") == _found(base, tasks[0], "Be careful.")
    assert _found(sim, tasks[1], "MARKER") == _found(base, tasks[1], "MARKER")


def test_a_wildcard_effect_applies_to_every_category() -> None:
    world = SimWorld([_task(1)], prompt_effects={"M": {"*": -0.3}})
    assert world.prompt_bonus("x M y", "code_review") == pytest.approx(-0.3)
    assert world.prompt_bonus("x y", "code_review") == 0.0


@pytest.mark.parametrize("axis", ["inv", "dom"])
def test_the_simulated_proposer_appends_a_hint_of_its_axis(axis: str) -> None:
    world = SimWorld([_task(1)], prompt_effects=sim_prompt_effects())
    sim = SimulatedProvider("p", {"m": SimModel()}, world)
    targets = [n for n, s in PROMPT_SPECS.items() if s.axis == axis]
    replies = []
    for index in range(3):
        request = ModelRequest(
            model_id="m",
            user_prompt="propose",
            metadata={
                "role": TUNE_PROPOSER_ROLE,
                "tune": {"axis": axis, "index": index, "targets": targets},
            },
        )
        reply = json.loads(sim._answer(request, sim.models["m"], TUNE_PROPOSER_ROLE, "", None))
        assert reply["target"] in targets
        assert reply["op"] == "append"
        replies.append(reply["text"])
    hints = [h.text for h in SIM_HINTS if h.axis == axis]
    assert replies == hints[:3]


def test_the_simulated_composer_keeps_the_strongest_edits() -> None:
    world = SimWorld([_task(1)])
    sim = SimulatedProvider("p", {"m": SimModel()}, world)
    edits = [
        {"target": "rules", "op": "append", "text": "A", "gain": 0.1},
        {"target": "claims", "op": "append", "text": "B", "gain": 0.05},
    ]
    for index, expected in ((0, ["A", "B"]), (1, ["A"]), (5, ["A"])):
        request = ModelRequest(
            model_id="m",
            user_prompt="compose",
            metadata={"role": TUNE_COMPOSER_ROLE, "tune": {"index": index, "edits": edits}},
        )
        reply = json.loads(sim._answer(request, sim.models["m"], TUNE_COMPOSER_ROLE, "", None))
        assert [e["text"] for e in reply["edits"]] == expected
