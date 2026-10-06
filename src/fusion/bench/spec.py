"""What a benchmark is made of: tasks with ground truth, arms, and the configuration of a study.

A dataset is a JSONL file (one ``BenchTask`` per line) or a YAML file (one task per document, or a
list). Tasks are data: nothing in a dataset says how it will be scored beyond its ``truth``, so the
same tasks serve every arm and every scorer.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fusion.bench.datasets.coding import (
    DEFAULT_TEST_COMMAND,
    CodingTaskError,
    is_task_dir,
    load_task_dir,
)
from fusion.config.layers import ConfigError
from fusion.routing.classifier import TaskType

__all__ = [
    "CATEGORY_TASK_TYPE",
    "DATASET_DIRS",
    "Arm",
    "BenchConfig",
    "BenchTask",
    "Category",
    "ArtifactTruth",
    "CodingFlaw",
    "CodingTruth",
    "Criterion",
    "EvidenceKind",
    "DatasetError",
    "DebugTruth",
    "Gate",
    "PerfSpec",
    "ReviewTruth",
    "RubricItem",
    "RubricTruth",
    "SeededBug",
    "SiteSpec",
    "Split",
    "Truth",
    "TruthPoint",
    "iter_rows",
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
    Path("evals") / "datasets" / "v1",  # tasks of the shipped dataset by name: ``coding``
    Path("evals") / "datasets",  # and the dataset itself: ``v1``
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


class CodingFlaw(BaseModel):
    """A plausible but wrong fix of a coding task: it applies, and some hidden test fails."""

    model_config = ConfigDict(extra="forbid")

    summary: str = ""  # what this fix does, in a sentence (simulated models repeat it as a claim)
    patch: str  # a unified diff against the task's files


class CodingTruth(BaseModel):
    """Ground truth of an executable coding task: tests, not keywords.

    The model's patch is applied to ``BenchTask.files`` in a sandbox, ``hidden_files`` are copied
    in (over anything of the same name), and ``command`` runs. ``expected_pass`` lists the ids of
    the tests a correct patch passes; quality is the share of them that pass. ``solution`` and
    ``flaws`` are for the validator and for simulated models; a real model never sees them.
    """

    model_config = ConfigDict(extra="allow")

    expected_pass: list[str] = Field(min_length=1)
    hidden_files: dict[str, str] = Field(min_length=1)
    command: list[str] = Field(default_factory=lambda: list(DEFAULT_TEST_COMMAND), min_length=1)
    timeout_s: float = Field(default=30.0, gt=0)
    mem_mb: int = Field(default=1024, ge=64)
    reruns: int = Field(default=2, ge=0, le=5)  # flake guard: extra runs when a test fails
    solution: str = ""
    solution_summary: str = ""
    flaws: list[CodingFlaw] = Field(default_factory=list)


EvidenceKind = Literal[
    "tests", "build", "static", "perf", "screenshot", "a11y", "console", "diff_stats"
]
CriterionSource = Literal["tests", "static", "perf", "a11y", "visual", "judge"]
ARTIFACT_KEYS = frozenset({"evaluators", "hard_gates", "soft_criteria", "perf", "site"})


class Gate(BaseModel):
    """A hard gate: a fact about the answer's artefacts that must hold, or it completes nothing.

    With no ``metric`` the evidence's own verdict (``ok``) must be true. With one, that metric
    must be at most ``max`` and at least ``min``. A gate whose evidence could not be taken (a
    browser that is not installed) is *unverified*, which does not fail it.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    evidence: EvidenceKind
    metric: str | None = None
    max: float | None = None
    min: float | None = None
    description: str = ""

    @model_validator(mode="after")
    def _bounds_need_a_metric(self) -> Gate:
        if self.metric is None and (self.max is not None or self.min is not None):
            msg = f"gate '{self.id}': max/min need a metric"
            raise ValueError(msg)
        if self.metric is not None and self.max is None and self.min is None:
            msg = f"gate '{self.id}': a metric needs a max or a min"
            raise ValueError(msg)
        return self


class Criterion(BaseModel):
    """A weighted soft criterion. ``source`` says what measures it without a judge; a criterion
    whose source is ``judge`` (or ``visual``, without a browser) is scored by the judge only and
    left out when there is none."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = ""
    weight: float = Field(default=1.0, gt=0)
    source: CriterionSource = "judge"


class PerfSpec(BaseModel):
    """How a performance task is measured.

    ``script`` is a hidden file defining ``setup(size)`` (build the input) and ``run(state)`` (the
    workload being timed). It runs for each of ``sizes``, in a fresh process, after ``warmup``
    unmeasured runs and ``samples`` measured ones. The answer must stay within ``max_ratio`` of the
    reference solution's median at the largest size and must not scale worse than the reference
    by more than ``scaling_slack`` (the exponent of time against size).
    """

    model_config = ConfigDict(extra="forbid")

    script: str = "tests/bench_workload.py"
    sizes: list[int] = Field(min_length=2)
    samples: int = Field(default=5, ge=5)
    warmup: int = Field(default=1, ge=0)
    timeout_s: float = Field(default=60.0, gt=0)
    max_ratio: float = Field(default=2.5, gt=1.0)
    scaling_slack: float = Field(default=0.5, ge=0.0)
    max_noise: float = Field(default=0.35, gt=0.0)  # MAD / median above this is "unstable"

    @field_validator("sizes")
    @classmethod
    def _increasing(cls, sizes: list[int]) -> list[int]:
        if sorted(set(sizes)) != sizes or sizes[0] < 1:
            msg = "perf sizes must be positive and strictly increasing"
            raise ValueError(msg)
        return sizes


class SiteSpec(BaseModel):
    """A frontend task's page: where it starts and how it is looked at."""

    model_config = ConfigDict(extra="forbid")

    entry: str = "index.html"
    timeout_s: float = Field(default=20.0, gt=0)


class ArtifactTruth(BaseModel):
    """What an executable frontend or performance task is judged on, beside its hidden tests.

    ``evaluators`` names the evaluators to run (default: by category). ``hard_gates`` must all
    hold; ``soft_criteria`` are weighted. Both live in ``truth`` of the compiled task.
    """

    model_config = ConfigDict(extra="allow")

    evaluators: list[str] = Field(default_factory=list)
    hard_gates: list[Gate] = Field(default_factory=list)
    soft_criteria: list[Criterion] = Field(default_factory=list)
    perf: PerfSpec | None = None
    site: SiteSpec | None = None

    @model_validator(mode="after")
    def _unique_ids(self) -> ArtifactTruth:
        ids = [g.id for g in self.hard_gates] + [c.id for c in self.soft_criteria]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            msg = f"gate and criterion ids must be unique; repeated: {', '.join(dupes)}"
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
        if "expected_pass" in self.truth:
            CodingTruth.model_validate(self.truth)
        if ARTIFACT_KEYS & self.truth.keys():
            ArtifactTruth.model_validate(self.truth)
        return self

    @property
    def task_type(self) -> TaskType:
        return CATEGORY_TASK_TYPE[self.category]

    @property
    def expects_patch(self) -> bool:
        """Whether the answer is a code patch (an executable coding task) rather than prose."""
        return "expected_pass" in self.truth

    def artifact_truth(self) -> ArtifactTruth:
        """The gates, criteria and measurement settings of a frontend or performance task."""
        return ArtifactTruth.model_validate(self.truth)

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
            or not (
                {"bugs", "root_cause_tags", "required_points", "forbidden_points", "expected_pass"}
                & t.keys()
            )
        ):
            return self.parsed_truth()
        if "expected_pass" in t:
            return self._simulated_coding_truth()
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

    def _simulated_coding_truth(self) -> Truth:
        """The fix as a point a model finds and each flaw as a decoy it falls for: simulated
        models then state them as claims and attach the matching patch (``simulated_coding``)."""
        coding = CodingTruth.model_validate(self.truth)
        points: list[dict[str, Any]] = []
        if coding.solution:
            said = coding.solution_summary or f"Apply the reference fix for {self.id}"
            points.append(
                {"id": "solution", "keywords": [said], "text": said, "kind": "recommendation"}
            )
        decoys = [
            {
                "id": f"flaw-{n}",
                "keywords": [flaw.summary or f"Alternative fix {n} for {self.id}"],
                "text": flaw.summary or f"Alternative fix {n} for {self.id}",
                "kind": "recommendation",
            }
            for n, flaw in enumerate(coding.flaws, 1)
        ]
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
    # Tasks to use per category (a seeded sample of each); categories not listed are left out.
    # With ``limit`` as well, ``limit`` then thins the result evenly across categories.
    quota: dict[Category, int] = Field(default_factory=dict)
    # Stop before cumulative live spend would pass this (the roadmap-wide cap minus a reserve);
    # ``None``: the ledger's own cap.
    spend_stop_usd: float | None = Field(default=None, gt=0)

    @field_validator("quota")
    @classmethod
    def _positive_quota(cls, quota: dict[Category, int]) -> dict[Category, int]:
        bad = sorted(c for c, n in quota.items() if n < 1)
        if bad:
            msg = f"quota counts must be at least 1; check: {', '.join(bad)}"
            raise ValueError(msg)
        return quota

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
            if candidate.is_file() or (suffix == "" and candidate.is_dir()):
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


def dataset_sources(path: Path) -> list[Path]:
    """The files and coding-task directories a dataset is made of, in a stable order.

    A directory holding ``task.yaml`` is one coding task and nothing under it is read as rows.
    """
    if path.is_file() or is_task_dir(path):
        return [path]
    found: list[Path] = []
    for root, dirs, names in os.walk(path):
        dirs.sort()
        here = Path(root)
        kept = []
        for name in dirs:
            if is_task_dir(here / name):
                found.append(here / name)
            else:
                kept.append(name)
        dirs[:] = kept
        found.extend(here / n for n in sorted(names) if Path(n).suffix in _SUFFIXES)
    return sorted(found)


def iter_rows(path: Path) -> Iterator[tuple[Path, int, Any]]:
    """``(source, index, row)`` for every task row of the dataset at ``path``: the rows of each
    JSONL or YAML file, and the row compiled from each coding-task directory (index 1)."""
    for source in dataset_sources(path):
        if source.is_dir():
            try:
                yield source, 1, load_task_dir(source)
            except CodingTaskError as exc:
                raise DatasetError(str(exc)) from exc
        else:
            for index, row in enumerate(read_rows(source), 1):
                yield source, index, row


def load_dataset(name_or_path: str | Path, split: str | None = None) -> list[BenchTask]:
    """Read and validate every task of a dataset (a file, or a directory of files and coding tasks).

    ``split`` ("dev" or "test") keeps that split's tasks and those with no split; None or "all"
    keeps everything.
    """
    path = resolve_dataset(name_or_path)
    if not dataset_sources(path):
        msg = f"No .jsonl or .yaml files or coding tasks in {path}"
        raise DatasetError(msg)
    tasks: list[BenchTask] = []
    for source, index, row in iter_rows(path):
        try:
            tasks.append(BenchTask.model_validate(row))
        except ValueError as exc:
            msg = f"{source} task {index}: {exc}"
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


def select_tasks(
    tasks: list[BenchTask],
    limit: int | None,
    seed: int = 0,
    quota: Mapping[Category, int] | None = None,
) -> list[BenchTask]:
    """``limit`` tasks spread across categories (round robin over a seeded shuffle).

    ``quota`` first takes that many tasks of each category it names (a seeded sample; fewer if the
    category has fewer) and drops the other categories. The result keeps the dataset's order, so
    the same arguments always pick the same tasks.
    """
    rng = random.Random(f"select-tasks:{seed}")  # noqa: S311 — reproducible sampling, not security
    if quota:
        by_cat: dict[str, list[BenchTask]] = {}
        for task in tasks:
            by_cat.setdefault(task.category, []).append(task)
        taken: set[str] = set()
        for category, count in sorted(quota.items()):
            pool = by_cat.get(category, [])
            taken.update(t.id for t in rng.sample(pool, min(count, len(pool))))
        tasks = [t for t in tasks if t.id in taken]
    if limit is None or limit >= len(tasks):
        return list(tasks)
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
