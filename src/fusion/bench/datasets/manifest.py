"""A dataset's own files beside its tasks: release rules, the frozen split and the owner's review.

Files whose name starts with ``_`` are never read as task rows (``spec.dataset_sources``). Three
live at a dataset's root:

* ``_dataset.yaml``: ``release:``, the coverage ``fusion bench dataset validate --release``
  holds the dataset to (``Rules`` field names; the v1 numbers are the defaults), so each dataset
  states its own promise as data.
* ``_splits.json``: the frozen split, ``{"frozen_on": date, "splits": {task id: dev | test}}``.
  Written once by ``fusion bench dataset freeze`` before any live run; afterwards a task whose
  split differs, or a task that is not in it, is an error (``freeze --add`` takes in new tasks
  with the split they declare). Tasks may still be fixed: the lock holds splits, not contents.
* ``_review.yaml``: the owner's review checklist, one entry per task with a ``decision``
  (``pending``, ``accept``, ``fix``, ``drop``) and a ``note``. ``fusion bench dataset review``
  creates it and keeps it in step with the tasks; a person fills in the decisions.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import fields
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fusion.bench.spec import BenchTask, DatasetError

__all__ = [
    "MANIFEST_FILE",
    "REVIEW_DECISIONS",
    "REVIEW_FILE",
    "SPLITS_FILE",
    "DatasetManifest",
    "ReviewEntry",
    "ReviewSheet",
    "SplitLock",
    "check_review",
    "check_splits",
    "freeze_splits",
    "load_manifest",
    "load_review",
    "load_splits",
    "review_progress",
    "update_review",
]

MANIFEST_FILE = "_dataset.yaml"
SPLITS_FILE = "_splits.json"
REVIEW_FILE = "_review.yaml"
Decision = Literal["pending", "accept", "fix", "drop"]
REVIEW_DECISIONS: tuple[str, ...] = ("pending", "accept", "fix", "drop")
_REVIEW_HEADER = """\
# Owner review of every task in this dataset: the 0.2.0 tasks were never reviewed by a person.
#
# For each task set `decision` and, unless you accept it as it is, say why in `note`:
#   accept   the task is sound: one clear ground truth, answerable from what it shows
#   fix      something is wrong; the note says what (the executor edits the task, then accepts)
#   drop     remove the task (the executor deletes it and keeps this entry as the record)
#   pending  not reviewed yet
# How to review a task: evals/datasets/README.md (guidelines per kind of task). Keep the file in
# step with the tasks with `uv run fusion bench dataset review evals/datasets/v2`, which adds new
# tasks as pending and never changes a decision.
"""


class DatasetManifest(BaseModel):
    """``_dataset.yaml``: what the dataset promises."""

    model_config = ConfigDict(extra="forbid")

    name: str = ""
    description: str = ""
    release: dict[str, Any] = Field(default_factory=dict)  # ``Rules`` fields


class SplitLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frozen_on: date
    splits: dict[str, Literal["dev", "test"]]


class ReviewEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: str = ""
    provenance: str = ""
    decision: Decision = "pending"
    note: str = ""


class ReviewSheet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[ReviewEntry] = Field(default_factory=list)


def _root(path: Path) -> Path:
    return path if path.is_dir() else path.parent


def load_manifest(path: Path) -> DatasetManifest | None:
    """The manifest at the dataset's root, or None when there is none."""
    file = _root(path) / MANIFEST_FILE
    if not file.is_file():
        return None
    try:
        manifest = DatasetManifest.model_validate(
            yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        )
    except (yaml.YAMLError, ValidationError) as exc:
        msg = f"{file}: {exc}"
        raise DatasetError(msg) from exc
    from fusion.bench.datasets.validate import Rules  # the field names the release block may use

    unknown = sorted(set(manifest.release) - {f.name for f in fields(Rules)})
    if unknown:
        msg = f"{file}: unknown release rule(s): {', '.join(unknown)}"
        raise DatasetError(msg)
    return manifest


def load_splits(path: Path) -> SplitLock | None:
    file = _root(path) / SPLITS_FILE
    if not file.is_file():
        return None
    try:
        return SplitLock.model_validate(json.loads(file.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValidationError) as exc:
        msg = f"{file}: {exc}"
        raise DatasetError(msg) from exc


def freeze_splits(path: Path, tasks: list[BenchTask], *, add: bool = False) -> SplitLock:
    """Write ``_splits.json`` from the tasks' declared splits. An existing lock is kept as it is:
    ``add`` takes in only the tasks it does not list yet; without it, freezing again refuses."""
    existing = load_splits(path)
    if existing is not None and not add:
        msg = f"{_root(path) / SPLITS_FILE} exists: the split is frozen (use --add for new tasks)"
        raise DatasetError(msg)
    unlabelled = sorted(t.id for t in tasks if t.split is None)
    if unlabelled:
        msg = f"tasks without a split cannot be frozen: {', '.join(unlabelled[:6])}"
        raise DatasetError(msg)
    splits = dict(existing.splits) if existing else {}
    for task in tasks:
        assert task.split is not None
        splits.setdefault(task.id, task.split)
    lock = SplitLock(
        frozen_on=existing.frozen_on if existing else date.today(),
        splits=dict(sorted(splits.items())),
    )
    body = json.dumps(lock.model_dump(mode="json"), indent=1, sort_keys=True)
    (_root(path) / SPLITS_FILE).write_text(body + "\n", encoding="utf-8")
    return lock


def check_splits(lock: SplitLock, tasks: list[BenchTask]) -> list[tuple[str, str]]:
    """``(task id, problem)`` for every task the frozen split does not hold as it is."""
    found: list[tuple[str, str]] = []
    for task in tasks:
        locked = lock.splits.get(task.id)
        if locked is None:
            found.append((task.id, "is not in the frozen split (bench dataset freeze --add)"))
        elif task.split != locked:
            said = f"the split frozen on {lock.frozen_on} says {locked}"
            found.append((task.id, f"is in {task.split} but {said}: a frozen split never changes"))
    return found


def load_review(path: Path) -> ReviewSheet | None:
    file = _root(path) / REVIEW_FILE
    if not file.is_file():
        return None
    try:
        return ReviewSheet.model_validate(yaml.safe_load(file.read_text(encoding="utf-8")) or {})
    except (yaml.YAMLError, ValidationError) as exc:
        msg = f"{file}: {exc}"
        raise DatasetError(msg) from exc


def _provenance(task: BenchTask) -> str:
    return "real-repo" if "real-repo" in task.tags else "synthetic"


def update_review(path: Path, tasks: list[BenchTask]) -> ReviewSheet:
    """Create or refresh ``_review.yaml``: every task gets an entry (new ones ``pending``),
    decisions and notes are never changed, and entries of removed tasks stay as the record."""
    sheet = load_review(path) or ReviewSheet()
    known = {e.id: e for e in sheet.tasks}
    entries: list[ReviewEntry] = []
    for task in tasks:
        entry = known.pop(task.id, None) or ReviewEntry(id=task.id)
        entries.append(
            entry.model_copy(update={"category": task.category, "provenance": _provenance(task)})
        )
    entries.extend(known.values())  # removed tasks: their decision (drop) is the record
    sheet = ReviewSheet(tasks=entries)
    rows = [e.model_dump() for e in entries]
    text = yaml.safe_dump({"tasks": rows}, sort_keys=False, allow_unicode=True, width=100)
    (_root(path) / REVIEW_FILE).write_text(_REVIEW_HEADER + "\n" + text, encoding="utf-8")
    return sheet


def review_progress(sheet: ReviewSheet) -> dict[str, int]:
    counts: Counter[str] = Counter(e.decision for e in sheet.tasks)
    return {d: counts.get(d, 0) for d in REVIEW_DECISIONS}


def check_review(
    sheet: ReviewSheet, tasks: list[BenchTask], *, release: bool
) -> list[tuple[Literal["error", "warning"], str, str]]:
    """``(level, where, problem)`` for a checklist out of step with the tasks or not yet done."""
    found: list[tuple[Literal["error", "warning"], str, str]] = []
    ids = {t.id for t in tasks}
    listed = Counter(e.id for e in sheet.tasks)
    for dup in sorted(i for i, n in listed.items() if n > 1):
        found.append(("error", dup, f"listed {listed[dup]} times in {REVIEW_FILE}"))
    for task_id in sorted(ids - set(listed)):
        found.append(
            ("error", task_id, f"missing from {REVIEW_FILE} (fusion bench dataset review)")
        )
    for entry in sheet.tasks:
        if entry.id not in ids and entry.decision != "drop":
            found.append(("error", entry.id, f"in {REVIEW_FILE} but not in the dataset"))
        elif entry.id in ids and entry.decision == "drop":
            found.append(
                ("error" if release else "warning", entry.id, "the owner dropped it: delete it")
            )
        elif entry.decision == "fix":
            found.append(("warning", entry.id, f"the owner asked for a fix: {entry.note or '?'}"))
    pending = sum(1 for e in sheet.tasks if e.decision == "pending" and e.id in ids)
    if pending:
        found.append(
            ("warning", "dataset", f"{pending} of {len(ids)} tasks await the owner's review")
        )
    return found
