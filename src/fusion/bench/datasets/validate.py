"""Checking a dataset before anyone spends money on it.

``validate_dataset`` reads every row, reports every problem it finds (not just the first) and
computes the statistics ``fusion bench dataset stats`` prints. Always checked: each row's schema,
unique ids and prompts, bug files and line numbers inside the files supplied (and on added lines of
the diff, when the context has one), rubric and debugging truth that can actually be scored,
no secret-looking strings, a licence note next to the dataset, and that the dev and test splits
share no task, prompt or file set. ``Rules(release=True)`` adds the coverage a published dataset
promises: size per category, a language mix, clean changes among the reviews, both splits and all
difficulties in every category, and the size of each task.

Executable tasks (``evals/datasets/v1/{coding,frontend,performance}/<id>/``) are checked by running
them. For frontend and performance tasks see ``_check_artifact_variants``. For coding tasks: the
reference solution must pass every hidden test (twice, to catch flakiness), the unpatched files
must fail some, every flawed fix must apply and fail some, and the hidden files must not collide
with the files the model is shown. That is the proof the tests tell a right fix from a wrong one.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from fusion.bench.scoring import AnswerView, ScoreEnv, ScoringError, get_scorer
from fusion.bench.scoring.artifact import answer_tree, assess, shared_evaluators, workspace
from fusion.bench.scoring.calibration import seeded_pair
from fusion.bench.scoring.coding import run_hidden_tests, visible_pass_fraction
from fusion.bench.spec import (
    BenchTask,
    CodingTruth,
    DatasetError,
    ReviewTruth,
    RubricTruth,
    iter_rows,
    resolve_dataset,
)
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
    min_coding: int = 30  # executable coding tasks
    min_artifact: int = 12  # executable frontend tasks, and performance tasks, each
    min_per_split_artifact: int = 4  # per split, for those two categories (they are smaller)
    # A flawed answer that passes every test must still score at least this much lower than the
    # reference solution's completion score to count as wrong (frontend tasks).
    flaw_margin: float = 0.04
    min_solution_completion: float = 0.85
    run_code: bool = True  # run each coding task's solution, flaws and tests (under a second each)
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


def _check_coding_task(
    task: BenchTask, rules: Rules
) -> list[tuple[Literal["error", "warning"], str]]:
    """Run one executable task's reference solution, unpatched files and flaws; return the
    problems."""
    found: list[tuple[Literal["error", "warning"], str]] = []
    truth = CodingTruth.model_validate(task.truth)
    clash = sorted(set(truth.hidden_files) & set(task.files))
    if clash:
        found.append(("error", f"hidden files collide with files the model is shown: {clash}"))
    if not truth.solution:
        return [*found, ("error", "no reference/solution: the tests cannot be shown to pass")]
    if not truth.solution_summary:
        found.append(("error", "task.yaml needs a solution_summary (what the right fix is)"))
    for n, flaw in enumerate(truth.flaws, 1):
        if not flaw.summary:
            found.append(("error", f"flaw {n} has no summary"))
    if not truth.flaws:
        found.append(("warning", "no flaws: nothing shows that the tests reject a wrong fix"))
    right = run_hidden_tests(task, truth.solution)
    if not right.applied:
        return [*found, ("error", f"the reference solution does not apply: {right.patch_error}")]
    if right.failed or right.timed_out:
        shown = ", ".join(right.failed[:4])
        what = shown or "timed out"
        found.append(("error", f"the reference solution fails hidden tests: {what}"))
    if right.flaky:
        found.append(("error", f"flaky tests (their result changed between runs): {right.flaky}"))
    again = run_hidden_tests(task, truth.solution, cache=False)  # a real second run
    if again.failed:
        found.append(("error", "the reference solution passes once and fails on a rerun"))
    if task.category in ("frontend", "performance"):
        return [*found, *_check_artifact_variants(task, truth, rules)]
    bare = run_hidden_tests(task, None)
    if not bare.failed:
        found.append(("error", "the unpatched files already pass every hidden test"))
    for n, flaw in enumerate(truth.flaws, 1):
        result = run_hidden_tests(task, flaw.patch)
        if not result.applied:
            found.append(("error", f"flaw {n} does not apply: {result.patch_error}"))
        elif not result.failed:
            found.append(("error", f"flaw {n} passes every hidden test, so it is not wrong"))
    visible = visible_pass_fraction(task, None)
    if visible is None:
        found.append(("warning", "no visible tests: the verified arm cannot check this task"))
    return found


def _check_artifact_variants(
    task: BenchTask, truth: CodingTruth, rules: Rules | None = None
) -> list[tuple[Literal["error", "warning"], str]]:
    """Frontend and performance tasks: measure the reference solution, the unpatched files and each
    flaw with the task's evaluators, and require what tells a right answer from a wrong one.

    * the solution passes every gate (and scores at least ``min_solution_completion``);
    * the unpatched files do not: they fail a hidden test (frontend) or the benchmark (performance,
      where slow code is correct code);
    * every flaw fails a hidden test or a gate, or (frontend) scores measurably lower.

    A measurement that was too noisy to decide is a warning, not an error.
    """
    rules = rules or Rules()
    found: list[tuple[Literal["error", "warning"], str]] = []
    spec = task.artifact_truth()
    evaluators = shared_evaluators()
    performance = task.category == "performance"
    if performance:
        if spec.perf is None:
            return [("error", "a performance task needs a perf block")]
        if spec.perf.script not in truth.hidden_files:
            return [("error", f"perf.script {spec.perf.script} is not among the hidden files")]
    produced = set(evaluators.names_for(task))
    for gate in spec.hard_gates:
        kinds = {"screenshot": "visual"}.get(gate.evidence, gate.evidence)
        if kinds not in produced:
            found.append(
                (
                    "error",
                    f"gate '{gate.id}' reads {gate.evidence} evidence that "
                    "no evaluator of the task makes",
                )
            )

    async def measure(patch: str | None) -> Any:
        with workspace(answer_tree(task, patch)) as workdir:
            return await assess(task, workdir, evaluators)

    def run(patch: str | None) -> Any:
        return asyncio.run(measure(patch))

    right = run(truth.solution)
    failed = [g.id for g in right.gates if g.passed is False]
    if failed:
        found.append(("error", f"the reference solution fails gate(s): {', '.join(failed)}"))
    elif right.completion < rules.min_solution_completion:
        found.append(("error", f"the reference solution completes only {right.completion:.2f}"))
    solution_perf = next((e for e in right.evidence if e.kind == "perf"), None)
    if solution_perf is not None and solution_perf.status == "unstable":
        found.append(("warning", "the reference solution's timing was too noisy to verify"))
    bare = run(None)
    bare_failed = [g.id for g in bare.gates if g.passed is False]
    if performance:
        perf = next((e for e in bare.evidence if e.kind == "perf"), None)
        if perf is not None and perf.status == "unstable":
            found.append(("warning", "the unpatched code's timing was too noisy to verify"))
        elif "fast-enough" not in bare_failed and not _is_slow(bare):
            found.append(
                (
                    "error",
                    "the unpatched code already meets the benchmark: it cannot tell fast from slow",
                )
            )
    elif not bare_failed:
        found.append(("error", "the unpatched files already pass every gate"))
    for n, flaw in enumerate(truth.flaws, 1):
        got = run(flaw.patch)
        tests = next((e for e in got.evidence if e.kind == "tests"), None)
        applied = tests is None or tests.metrics.get("applied", 1.0) == 1.0
        if not applied:
            found.append(("error", f"flaw {n} does not apply: {tests.summary if tests else ''}"))
        elif any(g.passed is False for g in got.gates):
            continue  # a gate catches it
        elif not performance and got.completion <= right.completion - rules.flaw_margin:
            continue  # measurably worse
        else:
            found.append(
                (
                    "error",
                    f"flaw {n} passes every gate and scores {got.completion:.2f} "
                    f"(the solution {right.completion:.2f}), so it is not wrong",
                )
            )
    if visible_pass_fraction(task, None) is None:
        found.append(("warning", "no visible tests: the verified arm cannot check this task"))
    return found


def _is_slow(assessment: Any) -> bool:
    """Whether a performance task's perf evidence says the code is too slow."""
    perf = next((e for e in assessment.evidence if e.kind == "perf"), None)
    return perf is not None and perf.ok is False


def _check_coding(tasks: list[BenchTask], rules: Rules, add: Any) -> None:
    if not tasks:
        return
    # Timing needs a quiet machine: performance tasks are checked one at a time, after the rest.
    quick = [t for t in tasks if t.category != "performance"]
    timed = [t for t in tasks if t.category == "performance"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        for task, problems in zip(
            quick, pool.map(lambda t: _check_coding_task(t, rules), quick), strict=True
        ):
            for level, message in problems:
                add(level, task.id, message)
    for task in timed:
        for level, message in _check_coding_task(task, rules):
            add(level, task.id, message)


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
            need = (
                rules.min_per_split_artifact
                if category in ("frontend", "performance")
                else rules.min_per_split
            )
            for split in ("dev", "test"):
                if counts[split] < need:
                    add("error", category, f"{counts[split]} {split} tasks; at least {need} needed")


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
    coding = stats.by_category.get("coding", 0)
    if coding < rules.min_coding:
        add("error", "coding", f"{coding} tasks; at least {rules.min_coding} needed")
    for category in ("frontend", "performance"):
        n = stats.by_category.get(category, 0)
        if n < rules.min_artifact:
            add("error", category, f"{n} tasks; at least {rules.min_artifact} needed")
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
    try:
        for source, index, row in iter_rows(root):
            where = f"{source.name}:{index}" if source.is_file() else source.name
            try:
                tasks.append(BenchTask.model_validate(row))
            except ValidationError as exc:
                first = exc.errors()[0]
                loc = ".".join(str(p) for p in first["loc"])
                add("error", where, f"invalid task: {loc}: {first['msg']}")
    except DatasetError as exc:
        add("error", str(root), str(exc))
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
    if rules.run_code:
        _check_coding([t for t in tasks if t.expects_patch], rules, add)
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
