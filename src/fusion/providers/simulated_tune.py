"""`fusion tune --mock`: simulated proposers, composers and prompt effects.

A simulated model ignores prompt wording, so a tuned prompt could never change a simulated score.
For a tuning study ``SimWorld`` takes *planted* prompt effects: a marker sentence that, when a
panel call's prompt contains it, shifts the model's skill on tasks of the listed categories. The
simulated proposer appends one of ``SIM_HINTS`` (chosen by its index), so a mock tuning run shows
every verdict: a hint that helps, one that does nothing, one that hurts, and one that helps one
task type and hurts others (``confounded``). These effects are made up: a mock run demonstrates
the procedure, never that a real prompt edit works.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "SIM_HINTS",
    "TUNE_COMPOSER_ROLE",
    "TUNE_PROPOSER_ROLE",
    "SimHint",
    "sim_prompt_effects",
    "tune_reply",
]

TUNE_PROPOSER_ROLE = "tune_proposer"
TUNE_COMPOSER_ROLE = "tune_composer"


@dataclass(frozen=True)
class SimHint:
    """A sentence the simulated proposer adds, and what it does to simulated skill.

    ``effect`` maps a task category (or ``*`` for all) to the skill change it causes.
    """

    axis: str
    target: str
    text: str
    effect: Mapping[str, float] = field(default_factory=dict)


SIM_HINTS: tuple[SimHint, ...] = (
    SimHint(
        "inv",
        "rules",
        "Before you answer, find the input line each point rests on; drop a point you cannot "
        "tie to one.",
        {"*": 0.25},
    ),
    SimHint("inv", "claims", "Keep the summary to one sentence.", {}),
    SimHint(
        "inv",
        "rules",
        "Answer with at most five claims; leave out minor points.",
        {"code_review": 0.2, "debugging": 0.2, "architecture": -0.35, "planning": -0.35},
    ),
    SimHint(
        "dom",
        "role.architecture_advisor",
        "Name the option you reject and the condition under which you would choose it instead.",
        {"architecture": 0.35},
    ),
    SimHint(
        "dom",
        "role.debugging_hypothesis",
        "Settle on the first plausible cause and fix it.",
        {"debugging": -0.35},
    ),
    SimHint("dom", "task.default", "Prefer the standard library's names for APIs.", {}),
)


def sim_prompt_effects() -> dict[str, dict[str, float]]:
    """The planted effects of ``SIM_HINTS``, as ``SimWorld(prompt_effects=...)`` takes them."""
    return {hint.text: dict(hint.effect) for hint in SIM_HINTS if hint.effect}


def tune_reply(role: str, meta: Any) -> str | None:
    """The simulated proposer's or composer's answer, from the request's ``tune`` metadata."""
    if not isinstance(meta, Mapping):
        return None
    index = int(meta.get("index", 0))
    if role == TUNE_PROPOSER_ROLE:
        return _propose(str(meta.get("axis", "inv")), index, list(meta.get("targets") or []))
    if role == TUNE_COMPOSER_ROLE:
        return _compose(index, list(meta.get("edits") or []))
    return None


def _propose(axis: str, index: int, targets: Sequence[str]) -> str:
    hints = [h for h in SIM_HINTS if h.axis == axis] or list(SIM_HINTS)
    hint = hints[index % len(hints)]
    target = hint.target if hint.target in targets or not targets else targets[0]
    return json.dumps(
        {
            "target": target,
            "op": "append",
            "text": hint.text,
            "rationale": f"simulated {axis} hint {index % len(hints) + 1}",
        }
    )


def _compose(index: int, edits: Sequence[Mapping[str, Any]]) -> str:
    """Composer ``index`` keeps all but the ``index`` weakest helpful edits (at least one)."""
    order = sorted((-float(e.get("gain") or 0.0), i) for i, e in enumerate(edits))
    ranked = [edits[i] for _, i in order]
    kept = ranked[: max(len(ranked) - index, 1)] if ranked else []
    return json.dumps(
        {
            "edits": [
                {"target": e.get("target"), "op": e.get("op", "append"), "text": e.get("text")}
                for e in kept
            ],
            "rationale": f"simulated composer {index + 1}: {len(kept)} edit(s)",
        }
    )
