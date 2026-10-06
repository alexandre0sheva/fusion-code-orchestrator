"""Helpers for tests of frontend and performance tasks: the shipped tasks, copied and edited."""

from __future__ import annotations

import shutil
from pathlib import Path

from fusion.bench.evaluators import EvaluatorSet
from fusion.bench.spec import BenchTask, CodingTruth, load_dataset
from test_evaluators import FakeDriver

ROOT = Path(__file__).parents[1]
V1 = ROOT / "evals" / "datasets" / "v1"


def shipped(task_id: str) -> BenchTask:
    """One of the shipped frontend or performance tasks."""
    folder = "frontend" if task_id.startswith("fe-") else "performance"
    return next(t for t in load_dataset(V1 / folder) if t.id == task_id)


def solution_patch(task: BenchTask) -> str:
    return CodingTruth.model_validate(task.truth).solution


def flaw_patch(task: BenchTask, n: int = 1) -> str:
    return CodingTruth.model_validate(task.truth).flaws[n - 1].patch


def offline_kit(artifacts: Path | None = None) -> EvaluatorSet:
    """Evaluators with no browser, so the tests do not depend on Playwright being installed."""
    return EvaluatorSet(artifacts, driver=FakeDriver(available=False))


def dataset_of(tmp: Path, *task_ids: str) -> Path:
    """A dataset directory holding copies of the named shipped tasks (and a licence note)."""
    root = tmp / "ds"
    root.mkdir()
    (root / "README.md").write_text("Synthetic tasks. Licence: MIT.\n", encoding="utf-8")
    for task_id in task_ids:
        folder = "frontend" if task_id.startswith("fe-") else "performance"
        shutil.copytree(V1 / folder / task_id, root / task_id)
    return root
