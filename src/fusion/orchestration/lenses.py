"""Panel lenses: each member of a review or debugging panel covers one area of the task.

A lens says what its member reports (``owns``) and what it must leave alone (``ignores``); the
member still reads the whole input. The answers are then pieces of one result, added together by
the ``union`` aggregator, instead of rival answers to average. Lenses come from the ``lenses``
config section (``config/lenses.yaml``).

``assign_lenses`` gives each member of a run its lens: a member that names one keeps it, the rest
take the task type's remaining lenses in order. With fewer members than lenses a member holds
several (merged into one ``Lens``), so the panel still covers every area; with more, lenses repeat.

A model listed more than once in a strategy (a self-mixture) runs once per *seat*: the first copy
keeps its alias and the others become ``<alias>#2``, ``<alias>#3`` (``seat_alias``), registered as
copies of the catalog entry so prices, fallbacks and the ledger work unchanged. Seats exist only
to carry different lenses, so ``assign_lenses`` drops a seat that would repeat its model's lens.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from fusion.config.layers import ConfigError, format_validation_error, resolve_config
from fusion.orchestration.strategy import PanelMember
from fusion.routing.classifier import TaskType, canonical_task_key

__all__ = [
    "Lens",
    "LensBook",
    "assign_lenses",
    "base_alias",
    "combine_lenses",
    "load_lens_book",
    "seat_alias",
]

SEAT_MARK = "#"  # gpt-luna#2: the second seat of gpt-luna in one panel
LENS_TASK_TYPES = frozenset(
    {"code_review", "debugging", "implementation_plan", "architecture_decision"}
)


class Lens(BaseModel):
    """One area of a task that a panel member covers."""

    model_config = ConfigDict(extra="forbid")

    name: str
    task_types: list[str] = Field(min_length=1)
    instruction: str = Field(min_length=1)
    owns: list[str] = Field(min_length=1)
    ignores: list[str] = Field(default_factory=list)

    @field_validator("task_types")
    @classmethod
    def _known_task_types(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - LENS_TASK_TYPES)
        if unknown:
            msg = f"unknown task type(s) {unknown}; lenses serve {sorted(LENS_TASK_TYPES)}"
            raise ValueError(msg)
        return value

    @property
    def parts(self) -> list[str]:
        """The lens names merged into this one (one name for a plain lens)."""
        return self.name.split("+")


def combine_lenses(lenses: Sequence[Lens]) -> Lens:
    """One lens covering every area of ``lenses`` (a member that holds several)."""
    if len(lenses) == 1:
        return lenses[0]
    owns = [area for lens in lenses for area in lens.owns]
    owned = set(owns)
    ignores = list(dict.fromkeys(i for lens in lenses for i in lens.ignores if i not in owned))
    return Lens(
        name="+".join(lens.name for lens in lenses),
        task_types=sorted({t for lens in lenses for t in lens.task_types}),
        instruction="\n".join(f"- {lens.name}: {lens.instruction}" for lens in lenses),
        owns=owns,
        ignores=ignores,
    )


class LensBook:
    """The configured lenses, in file order."""

    def __init__(self, lenses: Mapping[str, Lens]) -> None:
        self.lenses = dict(lenses)

    def names(self) -> list[str]:
        return list(self.lenses)

    def get(self, name: str) -> Lens:
        """A lens by name; ``a+b`` is the merge of lenses ``a`` and ``b``."""
        parts = name.split("+")
        missing = [p for p in parts if p not in self.lenses]
        if missing:
            msg = f"Unknown lens '{missing[0]}'; defined lenses: {', '.join(self.names())}"
            raise ConfigError(msg)
        return combine_lenses([self.lenses[p] for p in parts])

    def for_task(self, task_type: TaskType | str) -> list[Lens]:
        """The lenses that serve ``task_type``, in the order they are given out."""
        key = canonical_task_key(task_type) if isinstance(task_type, TaskType) else task_type
        return [lens for lens in self.lenses.values() if key in lens.task_types]


class _LensConfig(BaseModel):
    lenses: dict[str, Lens] = Field(default_factory=dict)

    @field_validator("lenses", mode="before")
    @classmethod
    def _name_from_key(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: {**body, "name": key} if isinstance(body, dict) else body
                for key, body in value.items()
            }
        return value

    @field_validator("lenses")
    @classmethod
    def _plain_names(cls, value: dict[str, Lens]) -> dict[str, Lens]:
        bad = [name for name in value if not name or "+" in name or SEAT_MARK in name]
        if bad:
            msg = f"lens names may not be empty or contain '+' or '{SEAT_MARK}': {bad}"
            raise ValueError(msg)
        return value


def load_lens_book(path: Path | None = None) -> LensBook:
    """Load lenses from the layered configuration, or from one explicit YAML file."""
    resolved = None
    if path is not None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            msg = f"Expected a mapping in {path}"
            raise ConfigError(msg)
    else:
        resolved = resolve_config()
        raw = {"lenses": resolved.data.get("lenses") or {}}
    try:
        config = _LensConfig.model_validate(raw)
    except ValidationError as exc:
        raise format_validation_error(exc, section_prefix="", resolved=resolved) from exc
    return LensBook(config.lenses)


# ----------------------------------------------------------------------------------------- seats


def seat_alias(alias: str, seat: int) -> str:
    """The alias of the ``seat``-th copy of a model in one panel (the first keeps its alias)."""
    return alias if seat <= 1 else f"{alias}{SEAT_MARK}{seat}"


def base_alias(alias: str) -> str:
    """The catalog alias behind a seat alias (``gpt-luna#2`` -> ``gpt-luna``)."""
    return alias.split(SEAT_MARK, 1)[0]


def seated(members: Sequence[PanelMember]) -> list[PanelMember]:
    """``members`` with each repeat of a model renamed to its seat alias, in order."""
    seen: dict[str, int] = {}
    out: list[PanelMember] = []
    for member in members:
        alias = base_alias(member.model)
        seen[alias] = seen.get(alias, 0) + 1
        out.append(member.model_copy(update={"model": seat_alias(alias, seen[alias])}))
    return out


# ------------------------------------------------------------------------------------ assignment


def assign_lenses(
    task_type: TaskType | str,
    members: Sequence[PanelMember],
    book: LensBook | None = None,
) -> dict[str, Lens]:
    """Each member's lens for a run of ``task_type``, keyed by the member's alias (seat alias).

    Empty when the task type has no lenses or fewer than two members answer (one member covers
    everything). A member that names a lens of this task type keeps it; the others share the
    remaining lenses in order, several per member when there are more lenses than members, and
    cycle over all of them when there are more members. A member left out of the result is a
    redundant seat: the same model with a lens another of its seats already holds.
    """
    book = book or load_lens_book()
    available = book.for_task(task_type)
    if not available or len(members) < 2:
        return {}
    by_name = {lens.name: lens for lens in available}
    explicit = {m.model: m.lens for m in members if m.lens is not None and m.lens in by_name}
    free = [m for m in members if m.model not in explicit]
    remaining = [lens for lens in available if lens.name not in set(explicit.values())]
    held: dict[str, list[Lens]] = {alias: [by_name[name]] for alias, name in explicit.items()}
    if free:
        if remaining and len(remaining) >= len(free):
            for index, lens in enumerate(remaining):
                held.setdefault(free[index % len(free)].model, []).append(lens)
        else:
            pool = remaining + [lens for lens in available if lens not in remaining]
            for index, member in enumerate(free):
                held[member.model] = [pool[index % len(pool)]]
    assigned: dict[str, Lens] = {}
    taken: set[tuple[str, str]] = set()
    for member in members:
        lens = combine_lenses(held[member.model])
        key = (base_alias(member.model), lens.name)
        if key in taken:
            continue  # the same model already asks with this lens: a second call adds nothing
        taken.add(key)
        assigned[member.model] = lens
    return assigned
