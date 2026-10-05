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
    "DebugTruth",
    "ReviewTruth",
    "RubricItem",
    "RubricTruth",
    "SeededBug",
    "Split",
    "Truth",
    "TruthPoint",
    "load_dataset",
    "read_rows",
    "resolve_dataset",
    "select_tasks",
    "task_hash",
]

Category = Literal[
    "code_review", "debugging", "architecture", "planning", "coding", "frontend", "performance"
]
Difficulty = Literal["easy", "medium", "hard"]
Split = Literal["dev", "test"]  # dev tunes defaults; test is touched only by the final study

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


class SeededBug(BaseModel):
    """A defect planted in a code-review task's files."""

    model_config = ConfigDict(extra="forbid")

    file: str
    line: int = Field(ge=1)
    category: str  # canonical name, e.g. "sql-injection"; ``aliases`` are other ways to say it
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    description: str
    aliases: list[str] = Field(default_factory=list)


class ReviewTruth(BaseModel):
    """Ground truth of a code-review task. No bugs means a clean change: any finding is false."""

    model_config = ConfigDict(extra="allow")

    bugs: list[SeededBug]
    line_tolerance: int = Field(default=3, ge=0)  # a finding this near a bug's line reports it


class DebugTruth(BaseModel):
    """Ground truth of a debugging task: one root cause, and how to fix it."""

    model_config = ConfigDict(extra="allow")

    root_cause_tags: list[str] = Field(min_length=1)  # equivalent names for the root cause
    root_cause_aliases: dict[str, list[str]] = Field(default_factory=dict)  # tag -> other wordings
    root_cause: str = ""  # the cause in a sentence, for the judge's equivalence check
    fix_keywords: list[str] = Field(
        default_factory=list
    )  # each: one element of the fix; a|b = either


class RubricItem(BaseModel):
    """One checklist item an architecture or planning answer is held to."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    text: str = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)  # lets the item be checked without a judge
    gate: bool = False  # an answer that misses a gating item cannot pass
    weight: float = Field(default=1.0, gt=0)

    def statement(self) -> str:
        """What an answer that meets the item would say: its text, with a keyword added when the
        text names none (so a keyword check agrees with the item)."""
        if not self.keywords or any(k.lower() in self.text.lower() for k in self.keywords):
            return self.text
        return f"{self.text} ({self.keywords[0]})"


def _as_items(raw: Any, prefix: str) -> Any:
    if not isinstance(raw, list):
        return raw
    items = [{"text": r} if isinstance(r, str) else r for r in raw]
    return [
        {**r, "id": r.get("id") or f"{prefix}{n}"} if isinstance(r, dict) else r
        for n, r in enumerate(items, 1)
    ]


class RubricTruth(BaseModel):
    """Ground truth of an architecture or planning task: what an answer must and must not say."""

    model_config = ConfigDict(extra="allow")

    required_points: list[RubricItem] = Field(default_factory=list)
    forbidden_points: list[RubricItem] = Field(default_factory=list)

    @field_validator("required_points", mode="before")
    @classmethod
    def _required(cls, raw: Any) -> Any:
        return _as_items(raw, "r")

    @field_validator("forbidden_points", mode="before")
    @classmethod
    def _forbidden(cls, raw: Any) -> Any:
        return _as_items(raw, "f")

    @model_validator(mode="after")
    def _unique_ids(self) -> RubricTruth:
        ids = [i.id for i in (*self.required_points, *self.forbidden_points)]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            msg = f"rubric item ids must be unique; repeated: {', '.join(dupes)}"
            raise ValueError(msg)
        return self


def _keyword_in(text: str, keywords: list[str]) -> str:
    """The first keyword that ``text`` contains (the text itself when there are none)."""
    return next((k for k in keywords if k.lower() in text.lower()), text)


_SEVERITY_SIM_WEIGHT = {"critical": 3.0, "high": 2.0, "medium": 1.0, "low": 0.5}


def _false_alarm(task: BenchTask, review: ReviewTruth) -> dict[str, Any]:
    """A finding a careless reviewer would add: a place with no seeded bug, or (on a clean change)
    any place at all. Its line is far enough from every bug that no scorer matches it."""
    path = next(iter(task.files), "unknown.py")
    count = len(task.files.get(path, "").splitlines()) or 1
    near = {b.line for b in review.bugs if b.file == path}
    gap = review.line_tolerance + 1
    line = next((n for n in range(count, 0, -1) if all(abs(n - b) > gap for b in near)), 1)
    where = f"{path}:{line}"
    return {
        "id": "false-alarm",
        "keywords": [where],
        "text": f"{where} possible null dereference when the value is missing",
    }


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
    split: Split | None = None  # None: not part of a dev/test design (it is in every split)

    @model_validator(mode="after")
    def _truth_is_well_formed(self) -> BenchTask:
        # Each scorer's truth format is checked here, so a dataset fails to load rather than score.
        if {"points", "decoys"} & self.truth.keys():
            Truth.model_validate(self.truth)
        if "bugs" in self.truth:
            ReviewTruth.model_validate(self.truth)
        if "root_cause_tags" in self.truth:
            DebugTruth.model_validate(self.truth)
        if {"required_points", "forbidden_points"} & self.truth.keys():
            RubricTruth.model_validate(self.truth)
        return self

    @property
    def task_type(self) -> TaskType:
        return CATEGORY_TASK_TYPE[self.category]

    def parsed_truth(self) -> Truth:
        return Truth.model_validate(self.truth)

    def simulated_truth(self) -> Truth:
        """What simulated models know about the task, as points they can find and decoys they can
        fall for. A ``points`` truth is used as written; the other formats are translated so that
        a simulated answer states each fact the way its scorer reads it."""
        t = self.truth
        if (
            "points" in t
            or "decoys" in t
            or not ({"bugs", "root_cause_tags", "required_points", "forbidden_points"} & t.keys())
        ):
            return self.parsed_truth()
        points: list[dict[str, Any]] = []
        decoys: list[dict[str, Any]] = []
        if "bugs" in t:
            review = ReviewTruth.model_validate(t)
            for n, bug in enumerate(review.bugs, 1):
                where = f"{bug.file}:{bug.line}"
                points.append(
                    {
                        "id": f"b{n}",
                        "keywords": [where],
                        "text": f"{where} {bug.category}: {bug.description}",
                        "severity": bug.severity,
                        "file": bug.file,
                        "line": bug.line,
                        "weight": _SEVERITY_SIM_WEIGHT[bug.severity],
                    }
                )
            decoys.append(_false_alarm(self, review))
        if "root_cause_tags" in t:
            debug = DebugTruth.model_validate(t)
            tag = debug.root_cause_tags[0]
            cause = debug.root_cause or tag
            text = f"Root cause: {tag}. {cause}"
            points.append({"id": "cause", "keywords": [tag], "text": text, "weight": 3.0})
            if debug.fix_keywords:
                fixes = [k.split("|")[0] for k in debug.fix_keywords]
                text = f"Fix: use {', '.join(fixes)}"
                points.append({"id": "fix", "keywords": [fixes[0]], "text": text})
            decoys.append(
                {
                    "id": "wrong-cause",
                    "keywords": ["transient network glitch"],
                    "text": "Root cause: a transient network glitch; retry the call",
                }
            )
        if {"required_points", "forbidden_points"} & t.keys():
            rubric = RubricTruth.model_validate(t)
            for item in rubric.required_points:
                said = item.statement()
                points.append(
                    {
                        "id": item.id,
                        "keywords": [_keyword_in(said, item.keywords)],
                        "text": said,
                        "weight": item.weight,
                    }
                )
            for item in rubric.forbidden_points:
                said = item.statement()
                decoys.append(
                    {"id": item.id, "keywords": [_keyword_in(said, item.keywords)], "text": said}
                )
        return Truth.model_validate({"points": points, "decoys": decoys})


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
    # Which part of a dev/test dataset to use. ``dev`` (the default) never touches the held-out
    # ``test`` tasks; ``all`` is for the final study. Tasks without a split are always used.
    split: Literal["dev", "test", "all"] = "dev"

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


def read_rows(path: Path) -> list[Any]:
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


def load_dataset(name_or_path: str | Path, split: str | None = None) -> list[BenchTask]:
    """Read and validate every task of a dataset (a file, or a directory of files).

    ``split`` ("dev" or "test") keeps that split's tasks and those with no split; None or "all"
    keeps everything.
    """
    path = resolve_dataset(name_or_path)
    files = sorted(p for p in path.rglob("*") if p.suffix in _SUFFIXES) if path.is_dir() else [path]
    if not files:
        msg = f"No .jsonl or .yaml files in {path}"
        raise DatasetError(msg)
    tasks: list[BenchTask] = []
    for file in files:
        for index, row in enumerate(read_rows(file), 1):
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
    if split not in (None, "all"):
        tasks = [t for t in tasks if t.split in (None, split)]
    return tasks


def task_hash(task: BenchTask) -> str:
    """Fingerprint of everything that decides a task's answer or score."""
    # The split label says where a task is used, not what its answer or score is.
    blob = json.dumps(
        task.model_dump(mode="json", exclude={"split"}), sort_keys=True, ensure_ascii=False
    )
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
