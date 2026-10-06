"""``tests`` and ``build``: does the answer pass the task's hidden tests, and does it compile?

``TestsEvaluator`` reuses Task 16's machinery (``run_hidden_tests``: hidden files laid over the
answer, per-test results parsed, a flake guard that reruns failures, results memoised) so that a
patch is run once however many arms gave it. ``BuildEvaluator`` is the cheap first gate: every
Python file must compile and, where ``node`` is installed, every JavaScript file must pass
``node --check``. Both run in the sandbox, on a copy.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fusion.bench.evaluators.base import Evidence, Memo, read_tree, run_in_sandbox, tree_digest
from fusion.bench.patch import diff_files
from fusion.bench.spec import BenchTask, CodingTruth, task_hash
from fusion.bench.virtual import offload

__all__ = ["BuildEvaluator", "TestsEvaluator"]

_COMPILE_SCRIPT = """
import pathlib, sys
bad = 0
count = 0
for path in sorted(pathlib.Path('.').rglob('*.py')):
    if '__pycache__' in path.parts:
        continue
    count += 1
    try:
        compile(path.read_text(encoding='utf-8'), str(path), 'exec')
    except (SyntaxError, ValueError) as exc:
        bad += 1
        print(f'{path}: {type(exc).__name__}: {exc}')
print(f'files={count} errors={bad}')
sys.exit(1 if bad else 0)
"""
_MAX_JS_FILES = 20


def _short(test_id: str) -> str:
    """``Class.test_name`` of a dotted unittest id."""
    return ".".join(test_id.rsplit(".", 2)[-2:])


class TestsEvaluator:
    """Hidden tests, per test, with the flake guard. ``ok`` means every expected test passed."""

    __test__ = False  # not a pytest class
    name = "tests"

    def __init__(self) -> None:
        self._memo = Memo()

    async def run(self, workdir: Path, task: BenchTask) -> Evidence:
        tree = read_tree(workdir)
        return await offload(self._evaluate, tree, task)

    def _evaluate(self, tree: dict[str, str], task: BenchTask) -> Evidence:
        if "expected_pass" not in task.truth:
            return Evidence(
                kind="tests",
                name=self.name,
                status="skipped",
                summary="the task has no hidden tests",
            )
        key = f"{task_hash(task)}:{tree_digest(tree)}"
        return self._memo.get_or_run(key, lambda: self._measure(tree, task))

    def _measure(self, tree: dict[str, str], task: BenchTask) -> Evidence:
        # Imported here: the scoring package imports the evaluators, so the reverse edge is lazy.
        from fusion.bench.scoring.coding import run_hidden_tests, visible_pass_fraction

        truth = CodingTruth.model_validate(task.truth)
        patch = diff_files(task.files, tree, delete_missing=True)
        outcome = run_hidden_tests(task, patch or None)
        expected = len(truth.expected_pass)
        metrics = {
            "pass_fraction": outcome.quality,
            "passed": float(len(outcome.passed)),
            "expected": float(expected),
            "flaky": float(len(outcome.flaky)),
            "runs": float(outcome.runs),
            "applied": 1.0 if outcome.applied else 0.0,
        }
        visible = None if not outcome.applied else visible_pass_fraction(task, patch or None)
        if visible is not None and visible >= 0:
            metrics["visible_pass_fraction"] = visible
        if not outcome.applied:
            summary = f"the answer could not be applied: {outcome.patch_error}"
        elif outcome.timed_out:
            summary = f"the tests timed out ({outcome.passed.__len__()}/{expected} passed)"
        elif outcome.failed:
            summary = f"{len(outcome.passed)}/{expected} hidden tests pass; failing: " + ", ".join(
                t.rsplit(".", 2)[-2] + "." + t.rsplit(".", 1)[-1] for t in outcome.failed[:6]
            )
        else:
            summary = f"all {expected} hidden tests pass"
        return Evidence(
            kind="tests",
            name=self.name,
            ok=outcome.applied and not outcome.failed and not outcome.timed_out,
            metrics=metrics,
            summary=summary,
            seconds=outcome.seconds,
        )


class BuildEvaluator:
    """Syntax check of the answer's Python (and, with ``node``, JavaScript) files."""

    name = "build"

    def __init__(self) -> None:
        self._memo = Memo()

    async def run(self, workdir: Path, task: BenchTask) -> Evidence:
        tree = read_tree(workdir)
        return await offload(
            lambda: self._memo.get_or_run(
                f"{task_hash(task)}:{tree_digest(tree)}", lambda: self._measure(tree)
            )
        )

    def _measure(self, tree: dict[str, str]) -> Evidence:
        py = [p for p in tree if p.endswith(".py")]
        js = [p for p in tree if p.endswith((".js", ".mjs", ".cjs"))][:_MAX_JS_FILES]
        if not py and not js:
            return Evidence(
                kind="build", name=self.name, status="skipped", summary="no code files to compile"
            )
        problems: list[str] = []
        seconds = 0.0
        if py:
            result, _ = run_in_sandbox(tree, ["python", "-c", _COMPILE_SCRIPT], timeout_s=30)
            seconds += result.seconds
            if not result.ok:
                problems += [ln for ln in result.stdout.splitlines() if "Error" in ln][:5] or [
                    "python files do not compile"
                ]
        checked_js = 0
        if js and shutil.which("node"):
            for path in js:
                result, _ = run_in_sandbox(tree, ["node", "--check", path], timeout_s=15)
                seconds += result.seconds
                checked_js += 1
                if not result.ok:
                    first = (result.stderr.strip().splitlines() or ["syntax error"])[-1]
                    problems.append(f"{path}: {first}")
        metrics = {
            "python_files": float(len(py)),
            "js_files_checked": float(checked_js),
            "errors": float(len(problems)),
        }
        summary = "everything compiles" if not problems else "; ".join(problems[:4])
        return Evidence(
            kind="build",
            name=self.name,
            ok=not problems,
            metrics=metrics,
            summary=summary,
            seconds=seconds,
        )
