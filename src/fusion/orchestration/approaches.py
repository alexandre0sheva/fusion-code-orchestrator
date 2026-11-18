"""Panel approaches: each member answers the whole task from a different stance.

Where the answer is one whole solution (an implementation plan, an architecture decision, a
``fusion_ask`` answer, a patch) the members' answers are rivals, so the ``select`` aggregator
picks one. An approach is what makes the rivals differ: a stance to answer from, given to a member
the way a lens is (``lenses.py``), but every member still answers the *whole* task. Approaches come
from the ``approaches`` config section (``config/approaches.yaml``).

``assign_approaches`` gives each member its approach: a member that names one keeps it, the rest
take the task's remaining approaches in order, repeating when there are more members than
approaches. A task whose answer is a patch uses the ``patch`` approaches, whatever its type. A
model listed more than once (a self-mixture) runs once per seat, as with lenses
(``lenses.seated``); a seat that would repeat its model's approach is dropped.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from fusion.config.layers import ConfigError, format_validation_error, resolve_config
from fusion.orchestration.lenses import SEAT_MARK, base_alias
from fusion.orchestration.strategy import PanelMember
from fusion.routing.classifier import TaskType, canonical_task_key

__all__ = [
    "APPROACH_TASK_TYPES",
    "PATCH_KEY",
    "Approach",
    "ApproachBook",
    "assign_approaches",
    "load_approach_book",
]

PATCH_KEY = "patch"  # the task key of approaches for a task whose answer is a code change
APPROACH_TASK_TYPES = frozenset(
    {"implementation_plan", "architecture_decision", "default", PATCH_KEY}
)


class Approach(BaseModel):
    """One stance a panel member answers the whole task from."""

    model_config = ConfigDict(extra="forbid")

    name: str
    task_types: list[str] = Field(min_length=1)
    instruction: str = Field(min_length=1)

    @field_validator("task_types")
    @classmethod
    def _known_task_types(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - APPROACH_TASK_TYPES)
        if unknown:
            msg = (
                f"unknown task type(s) {unknown}; approaches serve {sorted(APPROACH_TASK_TYPES)} "
                "(review and debugging use lenses)"
            )
            raise ValueError(msg)
        return value


class ApproachBook:
    """The configured approaches, in file order."""

    def __init__(self, approaches: Mapping[str, Approach]) -> None:
        self.approaches = dict(approaches)

    def names(self) -> list[str]:
        return list(self.approaches)

    def get(self, name: str) -> Approach:
        if name not in self.approaches:
            msg = f"Unknown approach '{name}'; defined approaches: {', '.join(self.names())}"
            raise ConfigError(msg)
        return self.approaches[name]

    def for_task(self, task_type: TaskType | str) -> list[Approach]:
        """The approaches that serve ``task_type``, in the order they are given out."""
        key = canonical_task_key(task_type) if isinstance(task_type, TaskType) else task_type
        return [a for a in self.approaches.values() if key in a.task_types]

    def for_patch(self) -> list[Approach]:
        """The approaches for a task whose answer is a code patch."""
        return self.for_task(PATCH_KEY)

    def available(self, task_type: TaskType | str, *, patch: bool = False) -> list[Approach]:
        """What a run of ``task_type`` draws from: the patch approaches when it asks for a patch."""
        return (self.for_patch() if patch else []) or self.for_task(task_type)


class _ApproachConfig(BaseModel):
    approaches: dict[str, Approach] = Field(default_factory=dict)

    @field_validator("approaches", mode="before")
    @classmethod
    def _name_from_key(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: {**body, "name": key} if isinstance(body, dict) else body
                for key, body in value.items()
            }
        return value

    @field_validator("approaches")
    @classmethod
    def _plain_names(cls, value: dict[str, Approach]) -> dict[str, Approach]:
        bad = [name for name in value if not name or "+" in name or SEAT_MARK in name]
        if bad:
            msg = f"approach names may not be empty or contain '+' or '{SEAT_MARK}': {bad}"
            raise ValueError(msg)
        return value


def load_approach_book(path: Path | None = None) -> ApproachBook:
    """Load approaches from the layered configuration, or from one explicit YAML file."""
    resolved = None
    if path is not None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            msg = f"Expected a mapping in {path}"
            raise ConfigError(msg)
    else:
        resolved = resolve_config()
        raw = {"approaches": resolved.data.get("approaches") or {}}
    try:
        config = _ApproachConfig.model_validate(raw)
    except ValidationError as exc:
        raise format_validation_error(exc, section_prefix="", resolved=resolved) from exc
    return ApproachBook(config.approaches)


def assign_approaches(
    task_type: TaskType | str,
    members: Sequence[PanelMember],
    *,
    patch: bool = False,
    book: ApproachBook | None = None,
) -> dict[str, Approach]:
    """Each member's approach for a run of ``task_type``, keyed by the member's alias (seat alias).

    Empty when the task has no approaches or fewer than two members answer (one answer has no
    rival). A member that names an approach of this task keeps it; the others take the remaining
    approaches in order and cycle over all of them when there are more members. A member left out
    of the result is a redundant seat: the same model with an approach another of its seats holds.
    """
    book = book or load_approach_book()
    available = book.available(task_type, patch=patch)
    if not available or len(members) < 2:
        return {}
    by_name = {a.name: a for a in available}
    explicit = {m.model: by_name[m.approach] for m in members if m.approach in by_name}
    free = [m for m in members if m.model not in explicit]
    remaining = [a for a in available if a not in explicit.values()]
    pool = remaining + [a for a in available if a not in remaining]
    chosen = dict(explicit)
    for index, member in enumerate(free):
        chosen[member.model] = pool[index % len(pool)]
    assigned: dict[str, Approach] = {}
    taken: set[tuple[str, str]] = set()
    for member in members:
        approach = chosen[member.model]
        key = (base_alias(member.model), approach.name)
        if key in taken:
            continue  # the same model already answers from this stance: a second call adds nothing
        taken.add(key)
        assigned[member.model] = approach
    return assigned
