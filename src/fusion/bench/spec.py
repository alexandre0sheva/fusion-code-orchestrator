"""What a benchmark is made of: tasks with ground truth, arms, and the configuration of a study.

A dataset is a JSONL file (one ``BenchTask`` per line) or a YAML file (one task per document, or a
list). Tasks are data: nothing in a dataset says how it will be scored beyond its ``truth``, so the
same tasks serve every arm and every scorer.
"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fusion.config.layers import ConfigError
from fusion.routing.classifier import TaskType

__all__ = [
    "CATEGORY_TASK_TYPE",
    "DATASET_DIRS",
    "Arm",
    "BenchConfig",
    "BenchTask",
    "Category",
    "DatasetError",
    "Truth",
    "TruthPoint",
    "load_dataset",
    "resolve_dataset",
    "select_tasks",
    "task_hash",
]

Category = Literal[
    "code_review", "debugging", "architecture", "planning", "coding", "frontend", "performance"
]
Difficulty = Literal["easy", "medium", "hard"]

# The pipeline's task type for each benchmark category; the pipeline only knows the first four.
CATEGORY_TASK_TYPE: dict[str, TaskType] = {
    "code_review": TaskType.CODE_REVIEW,
    "debugging": TaskType.DEBUGGING,
    "architecture": TaskType.ARCHITECTURE_DECISION,
    "planning": TaskType.IMPLEMENTATION_PLAN,
    "coding": TaskType.DEFAULT,
    "frontend": TaskType.DEFAULT,
    "performance": TaskType.DEFAULT,
}

# Places a bare dataset name ("toy") is looked up, after the path as given.
DATASET_DIRS = (
    Path(__file__).parent / "datasets",
    Path("evals") / "datasets" / "bench",
    Path("evals") / "bench",
)
_SUFFIXES = (".jsonl", ".yaml", ".yml")


class DatasetError(ConfigError):
    """A dataset file that cannot be found or read."""


class TruthPoint(BaseModel):
    """One thing an answer should (a point) or should not (a decoy) say.

    ``keywords`` are alternatives: an answer says the point when it contains any of them,
    ignoring case. ``text`` is what a model that gets it right would write; it defaults to the
    first keyword and must contain one.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    keywords: list[str] = Field(min_length=1)
    text: str = ""
    kind: str = "finding"
    severity: str | None = None
    file: str | None = None
    line: int | None = None
    weight: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def _text_carries_a_keyword(self) -> TruthPoint:
        if not self.text:
            self.text = self.keywords[0]
        elif not any(k.lower() in self.text.lower() for k in self.keywords):
            msg = f"point '{self.id}': text must contain one of its keywords"
            raise ValueError(msg)
        return self


class Truth(BaseModel):
    """The ground truth the built-in scorer understands. Other keys are left to other scorers."""

    model_config = ConfigDict(extra="allow")

    points: list[TruthPoint] = Field(default_factory=list)
    decoys: list[TruthPoint] = Field(default_factory=list)


class BenchTask(BaseModel):
    """One task in a dataset."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    category: Category
    prompt: str = Field(min_length=1)
    context: str = ""
    files: dict[str, str] = Field(default_factory=dict)
    truth: dict[str, Any]  # category-specific ground truth (datasets: roadmap tasks 15 and 16)
    difficulty: Difficulty = "medium"
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _truth_is_well_formed(self) -> BenchTask:
        if {"points", "decoys"} & self.truth.keys():  # the built-in scorer's truth format
            Truth.model_validate(self.truth)
        return self

    @property
    def task_type(self) -> TaskType:
        return CATEGORY_TASK_TYPE[self.category]

    def parsed_truth(self) -> Truth:
        return Truth.model_validate(self.truth)


class Arm(BaseModel):
    """One contestant: a strategy (from ``strategies.yaml`` or config), optionally modified."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    strategy: str
    # Strategy fields to change for this arm; nested mappings are merged, other values replaced.
    overrides: dict[str, Any] = Field(default_factory=dict)


class BenchConfig(BaseModel):
    """Everything that defines a study. Stored with the run so it can be resumed."""

    model_config = ConfigDict(extra="forbid")

    dataset: Path
    arms: list[Arm] = Field(min_length=1)
    repeats: int = Field(default=3, ge=1)
    seed: int = 0  # repeat ``n`` (from 1) runs with seed ``seed + n - 1``
    max_usd: float = Field(gt=0)
    concurrency: int = Field(default=8, ge=1)
    judge_models: list[str] = Field(default_factory=list)  # catalog aliases for LLM scorers
    mock: bool = False  # run on simulated providers: free, deterministic, no API keys
    cache: bool = True  # reuse provider responses from earlier runs
    redact: bool = False  # keep secret redaction on (off in benchmark mode by default)
    limit: int | None = Field(default=None, ge=1)  # use this many tasks (stratified, seeded)

    @field_validator("arms")
    @classmethod
    def _unique_arm_names(cls, arms: list[Arm]) -> list[Arm]:
        names = [a.name for a in arms]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            msg = f"arm names must be unique; repeated: {', '.join(dupes)}"
            raise ValueError(msg)
        return arms


def resolve_dataset(name_or_path: str | Path) -> Path:
    """The dataset file or directory for a path, or for a bare name such as ``toy``."""
    given = Path(name_or_path).expanduser()
    if given.exists():
        return given
    for folder in DATASET_DIRS:
        for suffix in ("", *_SUFFIXES):
            candidate = folder / f"{given}{suffix}"
            if candidate.is_file():
                return candidate
    searched = ", ".join(str(d) for d in DATASET_DIRS)
    msg = f"Dataset '{name_or_path}' not found (looked at the path itself and in {searched})"
    raise DatasetError(msg)


def _read_file(path: Path) -> list[Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        rows: list[Any] = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                msg = f"{path}:{number}: not valid JSON ({exc.msg})"
                raise DatasetError(msg) from exc
        return rows
    docs = [d for d in yaml.safe_load_all(text) if d is not None]
    return [row for doc in docs for row in (doc if isinstance(doc, list) else [doc])]


def load_dataset(name_or_path: str | Path) -> list[BenchTask]:
    """Read and validate every task of a dataset (a file, or a directory of files)."""
    path = resolve_dataset(name_or_path)
    files = sorted(p for p in path.rglob("*") if p.suffix in _SUFFIXES) if path.is_dir() else [path]
    if not files:
        msg = f"No .jsonl or .yaml files in {path}"
        raise DatasetError(msg)
    tasks: list[BenchTask] = []
    for file in files:
        for index, row in enumerate(_read_file(file), 1):
            try:
                tasks.append(BenchTask.model_validate(row))
            except ValueError as exc:
                msg = f"{file} task {index}: {exc}"
                raise DatasetError(msg) from exc
    ids = [t.id for t in tasks]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        msg = f"Task ids must be unique; repeated: {', '.join(dupes)}"
        raise DatasetError(msg)
    prompts = [t.prompt for t in tasks]
    if len(set(prompts)) != len(prompts):
        msg = "Task prompts must be unique: the prompt is how simulated models recognise a task"
        raise DatasetError(msg)
    return tasks


def task_hash(task: BenchTask) -> str:
    """Fingerprint of everything that decides a task's answer or score."""
    blob = json.dumps(task.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def select_tasks(tasks: list[BenchTask], limit: int | None, seed: int = 0) -> list[BenchTask]:
    """``limit`` tasks spread across categories (round robin over a seeded shuffle).

    The result keeps the dataset's order, so the same limit and seed always pick the same tasks.
    """
    if limit is None or limit >= len(tasks):
        return list(tasks)
    rng = random.Random(f"select-tasks:{seed}")  # noqa: S311 — reproducible sampling, not security
    by_category: dict[str, list[BenchTask]] = {}
    for task in tasks:
        by_category.setdefault(task.category, []).append(task)
    queues = [rng.sample(group, len(group)) for _, group in sorted(by_category.items())]
    chosen: set[str] = set()
    while len(chosen) < limit:
        for queue in queues:
            if queue and len(chosen) < limit:
                chosen.add(queue.pop().id)
    return [t for t in tasks if t.id in chosen]
