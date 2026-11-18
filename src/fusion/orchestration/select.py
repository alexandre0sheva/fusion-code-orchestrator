"""The ``select`` aggregator: one cheap call picks the best of several whole answers.

Where every panel member answers the whole task (an implementation plan, an architecture decision,
a ``fusion_ask`` answer, a patch) the answers are rivals, so merging them with a synthesizer means
a model rewrites everything. ``select`` instead shows a cheap model the candidates, anonymised as
``c1``..``cN`` in an order shuffled by the run id (so neither a name nor a position is a tell), and
asks for an id and a few lines of why, at most ``SELECT_OUTPUT_TOKENS`` of output: about a tenth of
a synthesis. The chosen answer comes back as written. ``select+patch`` also takes up to
``PATCH_POINTS`` short points that other candidates cover and the chosen one lacks, and appends
them after it: a cheap union without a rewrite.

A choice that is not one of the ids is never guessed at: ``parse_selection`` returns None and the
caller falls back to a vote. The selector's call is recorded in the run's ledger like the panel's.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from fusion.config.loader import ModelEntry
from fusion.orchestration.ledger import CallGateway
from fusion.orchestration.prompts import build_select_prompt, select_schema
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.routing.budget import SELECT_OUTPUT_TOKENS
from fusion.routing.classifier import TaskType

__all__ = [
    "PATCH_POINTS",
    "SELECTOR_SYSTEM_PROMPT",
    "Candidate",
    "MissingPoint",
    "SelectionResult",
    "anonymise",
    "parse_selection",
    "render_selection",
    "run_selector",
]

PATCH_POINTS = 3  # the most extra points ``select+patch`` takes from the other candidates
_POINT_CHARS = 300
_WHY_CHARS = 600
SELECTOR_SYSTEM_PROMPT = (
    "You are a blind selector. You are shown anonymous candidate answers to one task and pick the "
    "best one by the criteria you are given, judging the content only. You never write a new "
    "answer. Return only valid JSON."
)
_FENCED_CHOICE = re.compile(r"```choice\s*\n\s*(?P<id>[A-Za-z]\d+)\s*\n?\s*```", re.IGNORECASE)


@dataclass(frozen=True)
class Candidate:
    """One panel answer as the selector sees it: an id and text, nothing about who wrote it."""

    id: str
    model: str  # kept for the run record; never shown to the selector
    text: str


class MissingPoint(BaseModel):
    """A point another candidate covers that the chosen one lacks (``select+patch``)."""

    source: str  # the candidate id it comes from
    point: str


class SelectionResult(BaseModel):
    """The selector's verdict."""

    choice_id: str
    why: str = ""
    runner_up: str | None = None
    missing: list[MissingPoint] = Field(default_factory=list)


def _seed(run_id: str) -> int:
    return int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:8], "big")


def _scrub(text: str, hide: Iterable[str]) -> str:
    """``text`` without the model names in ``hide`` (an answer may sign itself)."""
    for name in sorted({h for h in hide if h}, key=len, reverse=True):
        text = re.sub(re.escape(name), "[model]", text, flags=re.IGNORECASE)
    return text


def anonymise(
    answers: list[tuple[str, str]], run_id: str, *, hide: Iterable[str] = ()
) -> list[Candidate]:
    """``answers`` (``(model, text)``) as candidates ``c1``..``cN`` in an order shuffled by
    ``run_id``: the same run always gives the same order, and the order carries no information.

    ``hide`` lists model names (aliases, model ids) that are scrubbed from the texts.
    """
    names = list(hide)
    order = list(range(len(answers)))
    random.Random(_seed(run_id)).shuffle(order)
    return [
        Candidate(id=f"c{position}", model=answers[index][0], text=_scrub(answers[index][1], names))
        for position, index in enumerate(order, 1)
    ]


# ---------------------------------------------------------------------------------- the verdict


def _object(response: ModelResponse) -> dict[str, Any] | None:
    data: Any = response.parsed_json
    if isinstance(data, dict):
        return data
    text = response.content
    start, end = text.find("{"), text.rfind("}") + 1
    if 0 <= start < end:
        try:
            data = json.loads(text[start:end])
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None
    return None


def _known(value: object, valid: set[str]) -> str | None:
    if isinstance(value, str) and (cid := value.strip().lower()) in valid:
        return cid
    return None


def parse_selection(
    response: ModelResponse, valid_ids: Iterable[str], max_points: int = 0
) -> SelectionResult | None:
    """The selector's choice, or None when it did not name one of ``valid_ids``.

    Accepts the JSON object the prompt asks for, or prose ending in a fenced ``choice`` block.
    Extra points are kept only up to ``max_points`` and only when they name another candidate.
    """
    valid = {v.lower() for v in valid_ids}
    data = _object(response)
    if data is not None and (choice := _known(data.get("choice"), valid)):
        why = data.get("why")
        runner = _known(data.get("runner_up"), valid)
        return SelectionResult(
            choice_id=choice,
            why=why.strip()[:_WHY_CHARS] if isinstance(why, str) else "",
            runner_up=runner if runner != choice else None,
            missing=_points(data.get("missing"), valid, choice, max_points),
        )
    fenced = _FENCED_CHOICE.search(response.content)
    if fenced and (choice := _known(fenced.group("id"), valid)):
        return SelectionResult(
            choice_id=choice, why=response.content[: fenced.start()].strip()[:_WHY_CHARS]
        )
    return None


def _points(raw: object, valid: set[str], choice: str, limit: int) -> list[MissingPoint]:
    points: list[MissingPoint] = []
    for row in raw if isinstance(raw, list) and limit > 0 else []:
        if len(points) >= limit:
            break
        if not isinstance(row, dict):
            continue
        source, point = _known(row.get("from"), valid), row.get("point")
        if source and source != choice and isinstance(point, str) and point.strip():
            points.append(MissingPoint(source=source, point=point.strip()[:_POINT_CHARS]))
    return points


def render_selection(chosen_text: str, result: SelectionResult) -> str:
    """The chosen answer as written, then (``select+patch``) the points it lacked."""
    if not result.missing:
        return chosen_text
    lines = [
        chosen_text.rstrip(),
        "",
        "### Also worth adding (covered by other candidates, missing above)",
        *(f"- {m.point}" for m in result.missing),
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------- the call


async def run_selector(
    *,
    selector_model: str,
    registry_models: dict[str, ModelEntry],
    task_type: TaskType,
    material: str,
    candidates: list[Candidate],
    patch: bool,
    patch_points: int,
    gateway: CallGateway,
) -> ModelResponse:
    """One cheap call that names the best candidate. A failed call comes back with ``error`` set;
    the caller falls back to a vote."""
    entry = registry_models[selector_model]
    request = ModelRequest(
        model_id=entry.model_id,
        system_prompt=SELECTOR_SYSTEM_PROMPT,
        user_prompt=build_select_prompt(
            task_type=task_type,
            material=material,
            candidates=[(c.id, c.text) for c in candidates],
            patch=patch,
            patch_points=patch_points,
        ),
        max_tokens=min(entry.max_tokens, SELECT_OUTPUT_TOKENS),
        response_schema=select_schema(with_missing=patch_points > 0),
        response_schema_name="selection",
        metadata={"task_type": task_type.value, "role": "selector", "personality": "judge"},
    )
    return await gateway.call(stage="synthesis", alias=selector_model, request=request)
