"""Checking a dataset before anyone spends money on it.

``validate_dataset`` reads every row, reports every problem it finds (not just the first) and
computes the statistics ``fusion bench dataset stats`` prints. Always checked: each row's schema,
unique ids and prompts, bug files and line numbers inside the files supplied (and on added lines of
the diff, when the context has one), rubric and debugging truth that can actually be scored,
no secret-looking strings, a licence note next to the dataset, and that the dev and test splits
share no task, prompt or file set. ``Rules(release=True)`` adds the coverage a published dataset
promises: size per category, a language mix, clean changes among the reviews, both splits and all
difficulties in every category, and the size of each task.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from fusion.bench.scoring import AnswerView, ScoreEnv, ScoringError, get_scorer
from fusion.bench.scoring.calibration import seeded_pair
from fusion.bench.spec import BenchTask, ReviewTruth, RubricTruth, read_rows, resolve_dataset
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.telemetry.cost import PricingRegistry

__all__ = [
    "KNOWN_LANGUAGES",
    "DatasetStats",
    "Issue",
    "Report",
    "Rules",
    "validate_dataset",
]

KNOWN_LANGUAGES = frozenset({"python", "typescript", "javascript", "go", "rust", "java", "ruby"})
_SECRETS = {
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "API key (sk-)": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
}
_SEEDED_GOOD_MIN = 0.9  # the answer written from the truth must score at least this...
_SEEDED_FLAWED_MAX = 0.5  # ...and its flawed twin at most this, or the truth cannot be scored


@dataclass(frozen=True)
class Rules:
    """What must hold. The release numbers are the roadmap's promise for the v1 dataset."""

    release: bool = False
    min_total: int = 100
    min_per_category: int = 25
    min_per_split: int = 8  # per category, dev and test each
    min_per_difficulty: int = 3  # per category
    min_languages: int = 3
    min_clean_share: float = 0.2  # of code-review tasks
    min_lines: int = 30  # a review's diff, a debugging task's code, trace and logs
    max_lines: int = 400


@dataclass
class Issue:
    level: Literal["error", "warning"]
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.where}: {self.message}"


class DatasetStats(BaseModel):
    tasks: int = 0
    by_category: dict[str, int] = Field(default_factory=dict)
    by_difficulty: dict[str, dict[str, int]] = Field(default_factory=dict)  # category -> counts
    by_split: dict[str, dict[str, int]] = Field(default_factory=dict)  # category -> counts
    languages: dict[str, int] = Field(default_factory=dict)
    clean_reviews: int = 0
    reviews: int = 0
    lines: dict[str, list[int]] = Field(default_factory=dict)  # category -> [min, mean, max]


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    stats: DatasetStats = field(default_factory=DatasetStats)
    tasks: list[BenchTask] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


# -- reading -----------------------------------------------------------------------------------


def _files_of(path: Path) -> list[Path]:
    suffixes = {".jsonl", ".yaml", ".yml"}
    return sorted(p for p in path.rglob("*") if p.suffix in suffixes) if path.is_dir() else [path]


def _gutter(line: str) -> tuple[int | None, str] | None:
    """``(new line number or None, op)`` of a diff line in the task's gutter format."""
    if (
        len(line) < 6
        or line[4] != " "
        or line[5] not in "+- "
        or (len(line) > 6 and line[6] != " ")
    ):
        return None
    number = line[:4].strip()
    if number and not number.isdigit():
        return None
    return (int(number) if number else None), line[5]


def _diff_lines(context: str) -> dict[tuple[str, int], str]:
    """``{(path, new line): op}`` for a context of ``=== path ===`` sections of gutter lines."""
    found: dict[tuple[str, int], str] = {}
    path = None
    for line in context.splitlines():
        if line.startswith("=== ") and line.rstrip().endswith("==="):
            path = line[4:].rsplit(" (", 1)[0]
        elif path is not None and (g := _gutter(line)) and g[0] is not None:
            found[(path, g[0])] = g[1]
    return found


def _size(task: BenchTask) -> int | None:
    """The lines a task shows the model: a review's diff, a debugging task's code and traces."""
    if task.category == "code_review":
        return sum(1 for line in task.context.splitlines() if _gutter(line))
    if task.category == "debugging":
        text = "\n".join([task.context, *task.files.values()])
        return sum(1 for line in text.splitlines() if line.strip())
    return None


def _language(task: BenchTask) -> str | None:
    return next((t for t in task.tags if t in KNOWN_LANGUAGES), None)


def _text_of(task: BenchTask) -> str:
    return "\n".join([task.prompt, task.context, *task.files.values(), json.dumps(task.truth)])


# -- the checks --------------------------------------------------------------------------------


def _check_task(task: BenchTask, rules: Rules, add: Any) -> None:
    where = task.id
    for kind, pattern in _SECRETS.items():
        if pattern.search(_text_of(task)):
            add("error", where, f"looks like it contains a secret ({kind}); use a placeholder")
    if task.category == "code_review" and "bugs" in task.truth:
        truth = ReviewTruth.model_validate(task.truth)
        diff = _diff_lines(task.context)
        ids = set()
        for n, bug in enumerate(truth.bugs, 1):
            text = task.files.get(bug.file)
            if text is None:
                add("error", where, f"bug {n}: file '{bug.file}' is not among the task's files")
                continue
            count = len(text.splitlines())
            if not 1 <= bug.line <= count:
                add(
                    "error",
                    where,
                    f"bug {n}: line {bug.line} is outside {bug.file} ({count} lines)",
                )
            elif diff and diff.get((bug.file, bug.line)) != "+":
                add(
                    "error",
                    where,
                    f"bug {n}: {bug.file}:{bug.line} is not an added line of the diff",
                )
            key = (bug.file, bug.line, bug.category)
            if key in ids:
                add("error", where, f"bug {n} repeats an earlier bug")
            ids.add(key)
    elif task.category == "code_review":
        add("warning", where, "a code-review task without `bugs` truth is scored on its points")
    if task.category in ("architecture", "planning") and (
        {"required_points", "forbidden_points"} & task.truth.keys()
    ):
        rubric = RubricTruth.model_validate(task.truth)
        if len(rubric.required_points) < 4:
            add("warning", where, "fewer than four required points: the checklist is thin")
        if not rubric.forbidden_points:
            add("warning", where, "no forbidden points: nothing penalises bad advice")
        for item in (*rubric.required_points, *rubric.forbidden_points):
            if not item.keywords:
                add("warning", where, f"item {item.id} has no keywords: it needs a judge")
    if (
        rules.release
        and task.category == "debugging"
        and "root_cause_tags" in task.truth
        and not task.truth.get("fix_keywords")
    ):
        add("error", where, "a release debugging task needs fix_keywords")
    size = _size(task)
    if rules.release and size is not None and not rules.min_lines <= size <= rules.max_lines:
        add("error", where, f"{size} lines is outside {rules.min_lines}-{rules.max_lines}")


async def _scorability(tasks: list[BenchTask], add: Any) -> None:
    """The answer written from each task's truth must score well, and its flawed twin badly:
    otherwise the keywords, aliases or line numbers do not describe what they should."""
    env = ScoreEnv(
        gateway=CallGateway(
            ledger=RunLedger(lambda: 0.0),
            models={},
            providers={},
            pricing=PricingRegistry(),
            truncate_prompts=False,
        )
    )
    for task in tasks:
        pair = seeded_pair(task)
        if pair is None:
            continue
        scorer = get_scorer(task.category)
        try:
            good, flawed = [
                (await scorer.score(task, AnswerView(final_answer=text), env)).quality
                for text in pair
            ]
        except ScoringError as exc:  # a judge-only task: fine, but nothing offline can check it
            add("warning", task.id, f"not checked offline: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — a task that cannot be scored is the finding
            add("error", task.id, f"cannot be scored: {type(exc).__name__}: {exc}")
            continue
        if good < _SEEDED_GOOD_MIN:
            add("error", task.id, f"its own correct answer scores only {good:.2f}")
        if flawed > _SEEDED_FLAWED_MAX:
            add("error", task.id, f"a flawed answer still scores {flawed:.2f}")


def _check_splits(tasks: list[BenchTask], rules: Rules, add: Any) -> None:
    labelled = [t for t in tasks if t.split]
    if not labelled:
        if rules.release:
            add("error", "dataset", "no task has a dev/test split")
        return
    for t in tasks:
        if not t.split:
            add("error", t.id, "has no split, but other tasks do")
    by_prompt: dict[str, str] = {}
    by_files: dict[str, str] = {}
    for t in labelled:
        assert t.split is not None
        for registry, key, what in (
            (by_prompt, re.sub(r"\s+", " ", t.prompt).strip().lower(), "prompt"),
            (by_files, json.dumps(t.files, sort_keys=True) if t.files else "", "files"),
        ):
            if not key:
                continue
            seen = registry.setdefault(key, t.split)
            if seen != t.split:
                add("error", t.id, f"shares its {what} with a task in the {seen} split")
    if rules.release:
        for category in sorted({t.category for t in labelled}):
            counts = Counter(t.split for t in labelled if t.category == category)
            for split in ("dev", "test"):
                if counts[split] < rules.min_per_split:
                    add(
                        "error",
                        category,
                        f"{counts[split]} {split} tasks; at least {rules.min_per_split} needed",
                    )


def _check_coverage(tasks: list[BenchTask], stats: DatasetStats, rules: Rules, add: Any) -> None:
    if len(tasks) < rules.min_total:
        add("error", "dataset", f"{len(tasks)} tasks; at least {rules.min_total} needed")
    for category in ("code_review", "debugging", "architecture", "planning"):
        n = stats.by_category.get(category, 0)
        if n < rules.min_per_category:
            add("error", category, f"{n} tasks; at least {rules.min_per_category} needed")
        for difficulty in ("easy", "medium", "hard"):
            have = stats.by_difficulty.get(category, {}).get(difficulty, 0)
            if have < rules.min_per_difficulty:
                add(
                    "error",
                    category,
                    f"{have} {difficulty} tasks; at least {rules.min_per_difficulty} needed",
                )
    if len(stats.languages) < rules.min_languages:
        add(
            "error",
            "dataset",
            f"{len(stats.languages)} languages ({', '.join(stats.languages) or 'none'}); "
            f"at least {rules.min_languages} needed",
        )
    if stats.reviews and stats.clean_reviews / stats.reviews < rules.min_clean_share:
        add(
            "error",
            "code_review",
            f"{stats.clean_reviews} of {stats.reviews} reviews are clean; "
            f"at least {rules.min_clean_share:.0%} must be",
        )


def _has_licence_note(path: Path) -> bool:
    start = path if path.is_dir() else path.parent
    for folder in (start, *start.parents[:2]):
        readme = folder / "README.md"
        if readme.is_file() and "licen" in readme.read_text(encoding="utf-8").lower():
            return True
    return False


def _stats(tasks: list[BenchTask]) -> DatasetStats:
    stats = DatasetStats(tasks=len(tasks))
    stats.by_category = dict(sorted(Counter(t.category for t in tasks).items()))
    for category in stats.by_category:
        group = [t for t in tasks if t.category == category]
        stats.by_difficulty[category] = dict(Counter(t.difficulty for t in group))
        stats.by_split[category] = dict(Counter(t.split or "none" for t in group))
        sizes = [s for t in group if (s := _size(t)) is not None]
        if sizes:
            stats.lines[category] = [min(sizes), round(sum(sizes) / len(sizes)), max(sizes)]
    stats.languages = dict(sorted(Counter(lang for t in tasks if (lang := _language(t))).items()))
    reviews = [t for t in tasks if t.category == "code_review"]
    stats.reviews = len(reviews)
    stats.clean_reviews = sum(1 for t in reviews if t.truth.get("bugs") == [])
    return stats


def validate_dataset(path: str | Path, rules: Rules | None = None) -> Report:
    """Validate the dataset at ``path`` (a file, a directory, or a bare name) against ``rules``."""
    rules = rules or Rules()
    report = Report()

    def add(level: Literal["error", "warning"], where: str, message: str) -> None:
        report.issues.append(Issue(level, where, message))

    root = resolve_dataset(path)
    tasks: list[BenchTask] = []
    for file in _files_of(root):
        for index, row in enumerate(read_rows(file), 1):
            where = f"{file.name}:{index}"
            try:
                tasks.append(BenchTask.model_validate(row))
            except ValidationError as exc:
                first = exc.errors()[0]
                loc = ".".join(str(p) for p in first["loc"])
                add("error", where, f"invalid task: {loc}: {first['msg']}")
    if not tasks and not report.issues:
        add("error", str(root), "no tasks found")
    ids = Counter(t.id for t in tasks)
    for dup in sorted(i for i, n in ids.items() if n > 1):
        add("error", dup, f"id used {ids[dup]} times")
    prompts = Counter(re.sub(r"\s+", " ", t.prompt).strip().lower() for t in tasks)
    if any(n > 1 for n in prompts.values()):
        dupes = [t.id for t in tasks if prompts[re.sub(r"\s+", " ", t.prompt).strip().lower()] > 1]
        add(
            "error",
            ", ".join(dupes[:4]),
            "prompts must be unique (simulated models find a task by it)",
        )
    for task in tasks:
        _check_task(task, rules, add)
    asyncio.run(_scorability(tasks, add))
    _check_splits(tasks, rules, add)
    report.tasks = tasks
    report.stats = _stats(tasks)
    if rules.release:
        _check_coverage(tasks, report.stats, rules, add)
    if not _has_licence_note(root):
        add(
            "error" if rules.release else "warning",
            str(root),
            "no README.md with a licence note next to the dataset",
        )
    return report
