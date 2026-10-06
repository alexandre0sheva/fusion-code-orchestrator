"""``CodingScorer``: run a patch against a task's hidden tests, in a sandbox.

The answer's one patch (a unified diff or file blobs, see ``fusion.bench.patch``) is applied to the
task's files in a fresh ``Sandbox``, the hidden test files are copied over the result, and the
task's test command runs. Quality is pass@1: the share of ``expected_pass`` tests that pass. A
patch that cannot be found, read or applied scores 0, and the details say which.

**Flake guard.** When the first run does not pass every expected test it is run again, up to
``reruns`` (2) more times, each in a fresh sandbox, and a test counts as passed when it passed in
the majority of the runs. A run that times out is not repeated (a hang is not a flake). The
details list the tests whose result differed between runs as ``flaky``.

Results are memoised by (task, patch): the same patch from several arms, repeats or runs is run
once. A scorer spends no model money, so its ``cost_usd`` is always 0.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fusion.bench.patch import PatchError, apply_patch, extract_patch, patch_text_key
from fusion.bench.sandbox import RunResult, Sandbox, SandboxError, SandboxLimits
from fusion.bench.scoring.base import (
    AnswerView,
    Evidence,
    EvidenceItem,
    ScoreEnv,
    ScoreResult,
    ScoringError,
)
from fusion.bench.spec import BenchTask, CodingTruth, task_hash
from fusion.bench.virtual import offload
from fusion.routing.budget import PlannedCall

__all__ = [
    "CodingOutcome",
    "CodingScorer",
    "coding_verifier",
    "parse_test_results",
    "run_hidden_tests",
    "visible_pass_fraction",
]

PASSED = "passed"
_UNITTEST_ID = re.compile(r"^(?P<name>\w+) \((?P<id>[\w.]+)\)")
_UNITTEST_END = re.compile(
    r"\.\.\. (?P<status>ok|FAIL|ERROR|skipped\b.*|expected failure|unexpected success)\s*$"
)
_PYTEST_LINE = re.compile(
    r"^(?P<id>\S+::\S+)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b"
)
_PYTEST_SUMMARY = re.compile(r"^(?P<status>PASSED|FAILED|ERROR)\s+(?P<id>\S+::\S+)")
_CACHE_LIMIT = 4096
_SLOTS = threading.BoundedSemaphore(max(2, min(os.cpu_count() or 4, 8)))  # sandboxes at once
_outcomes: dict[str, CodingOutcome] = {}
_lock = threading.Lock()


_PYTEST_KIND = {
    "PASSED": "passed",
    "FAILED": "failed",
    "XPASS": "failed",
    "ERROR": "error",
    "SKIPPED": "skipped",
    "XFAIL": "skipped",
}
_UNITTEST_KIND = {
    "ok": "passed",
    "FAIL": "failed",
    "unexpected success": "failed",
    "ERROR": "error",
}


def parse_test_results(output: str) -> dict[str, str]:
    """``{test id: "passed" | "failed" | "error" | "skipped"}`` from ``unittest -v`` or
    ``pytest -v``/``-rA`` output. A test the output does not list is not in the result."""
    found: dict[str, str] = {}
    pending: str | None = None
    for raw in output.splitlines():
        line = raw.rstrip()
        match = _PYTEST_LINE.match(line) or _PYTEST_SUMMARY.match(line)
        if match:
            found.setdefault(match.group("id"), _PYTEST_KIND[match.group("status")])
            continue
        ident = _UNITTEST_ID.match(line)
        if ident:
            pending = ident.group("id")
        end = _UNITTEST_END.search(line)
        if end and pending:
            found[pending] = _UNITTEST_KIND.get(end.group("status"), "skipped")
            pending = None
    return found


@dataclass
class CodingOutcome:
    """What running one patch against a task's hidden tests found."""

    quality: float
    applied: bool
    patch_error: str = ""
    passed: list[str] = field(default_factory=list)  # expected tests that passed
    failed: list[str] = field(default_factory=list)  # expected tests that did not
    flaky: list[str] = field(default_factory=list)
    runs: int = 0
    timed_out: bool = False
    seconds: float = 0.0
    isolation: str = "none"
    tail: str = ""  # the end of the test output when no test result could be read

    @property
    def cacheable(self) -> bool:
        return not self.timed_out

    def details(self, expected: int) -> dict[str, Any]:
        info: dict[str, Any] = {
            "patch": "applied" if self.applied else "rejected",
            "passed": len(self.passed),
            "expected": expected,
            "runs": self.runs,
            "isolation": self.isolation,
            "seconds": round(self.seconds, 2),
        }
        if self.patch_error:
            info["error"] = self.patch_error
        if self.failed:
            info["failed"] = self.failed[:12]
        if self.flaky:
            info["flaky"] = self.flaky
        if self.timed_out:
            info["timed_out"] = True
        if self.tail:
            info["output_tail"] = self.tail
        return info

    def evidence(self) -> Evidence:
        """The same result as measurements, for scorers that show a judge what the tests said."""
        items = [EvidenceItem(kind="test", name=t, passed=True) for t in self.passed]
        items += [EvidenceItem(kind="test", name=t, passed=False) for t in self.failed]
        return Evidence(items=items)


# -- one run --------------------------------------------------------------------------------------


def _prepare(box: Sandbox, task: BenchTask, truth: CodingTruth | None, patch: str | None) -> str:
    """Files in, patch applied, hidden tests laid over. Returns a patch error, or ``""``."""
    box.copy_in(task.files)
    if patch:
        try:
            changes = apply_patch(task.files, patch)
            for path, content in changes.items():
                if content is None:
                    box.remove(path)
                else:
                    box.write(path, content)
        except (PatchError, SandboxError) as exc:
            return str(exc)
    if truth is not None:
        box.copy_in(truth.hidden_files)  # last: a patch cannot weaken the tests
    if not box.exists("tests/__init__.py"):
        box.write("tests/__init__.py", "")  # unittest discovery needs the start directory a package
    return ""


def _run_once(
    task: BenchTask, truth: CodingTruth, patch: str | None, *, hidden: bool
) -> tuple[str, RunResult | None, str]:
    """``(patch error, the command's result, isolation)`` for one run in a fresh sandbox."""
    limits = SandboxLimits(timeout_s=truth.timeout_s, mem_mb=truth.mem_mb)
    with _SLOTS, Sandbox(limits=limits) as box:
        error = _prepare(box, task, truth if hidden else None, patch)
        if error:
            return error, None, box.isolation
        return "", box.run(truth.command), box.isolation


def run_hidden_tests(task: BenchTask, patch: str | None, *, cache: bool = True) -> CodingOutcome:
    """Run ``patch`` (None: no change) against ``task``'s hidden tests. Blocking. The result is
    memoised by (task, patch); ``cache=False`` always runs the tests again."""
    key = f"{task_hash(task)}:{hashlib.sha256(patch_text_key(patch or '').encode()).hexdigest()}"
    if cache:
        with _lock:
            hit = _outcomes.get(key)
        if hit is not None:
            return hit
    outcome = _evaluate(task, patch)
    if outcome.cacheable:
        with _lock:
            if len(_outcomes) >= _CACHE_LIMIT:
                _outcomes.pop(next(iter(_outcomes)))
            _outcomes[key] = outcome
    return outcome


def _evaluate(task: BenchTask, patch: str | None) -> CodingOutcome:
    truth = CodingTruth.model_validate(task.truth)
    expected = truth.expected_pass
    error, result, isolation = _run_once(task, truth, patch, hidden=True)
    if result is None:
        return CodingOutcome(0.0, applied=False, patch_error=error, failed=list(expected))
    runs = [parse_test_results(result.output)]
    seconds = result.seconds
    timed_out = result.timed_out
    while (
        len(runs) <= truth.reruns
        and not timed_out
        and any(runs[0].get(t) != PASSED for t in expected)
    ):
        _, again, _ = _run_once(task, truth, patch, hidden=True)
        if again is None:
            break
        runs.append(parse_test_results(again.output))
        seconds += again.seconds
        timed_out = timed_out or again.timed_out
    need = len(runs) // 2 + 1
    passed = [t for t in expected if sum(r.get(t) == PASSED for r in runs) >= need]
    flaky = [t for t in expected if len({r.get(t) for r in runs}) > 1]
    return CodingOutcome(
        quality=len(passed) / len(expected),
        applied=True,
        passed=passed,
        failed=[t for t in expected if t not in passed],
        flaky=flaky,
        runs=len(runs),
        timed_out=timed_out,
        seconds=seconds,
        isolation=isolation,
        tail=result.output[-600:] if not runs[0] else "",
    )


def visible_pass_fraction(task: BenchTask, patch: str | None) -> float | None:
    """The share of the task's *visible* tests (the ones in its files, not the hidden ones) that
    pass with ``patch`` applied: what a benchmark-only verifier may look at. -1.0 when the patch
    does not apply, None when the task has no visible tests. Blocking."""
    truth = CodingTruth.model_validate(task.truth)
    error, result, _ = _run_once(task, truth, patch, hidden=False)
    if result is None:
        return -1.0 if error else None
    statuses = parse_test_results(result.output)
    counted = [s for s in statuses.values() if s != "skipped"]
    return counted.count(PASSED) / len(counted) if counted else None


def coding_verifier(task: BenchTask) -> Callable[[str], Awaitable[float | None]] | None:
    """The function the ``verified`` aggregator calls to run ``task``'s visible tests on a patch
    (None for a task that is not a coding task). Benchmark only: it runs code."""
    if not task.expects_patch:
        return None

    async def verify(patch: str) -> float | None:
        return await offload(visible_pass_fraction, task, patch)

    return verify


# -- the scorer -----------------------------------------------------------------------------------


class CodingScorer:
    """Pass@1 of a patch against the task's hidden tests. Falls back to points for a task with
    only ``points`` truth."""

    name = "tests"

    def estimate_calls(self, task: BenchTask, judges: list[str]) -> list[PlannedCall]:
        return []  # no model is asked: the tests are the judge

    async def score(
        self,
        task: BenchTask,
        answer: AnswerView,
        env: ScoreEnv,
        evidence: Evidence | None = None,
    ) -> ScoreResult:
        if "expected_pass" not in task.truth:
            from fusion.bench.scoring.points import points_fallback

            return await points_fallback(task, self.name).score(task, answer, env, evidence)
        expected = len(CodingTruth.model_validate(task.truth).expected_pass)
        found = extract_patch(answer.final_answer, answer.structured)
        if found.text is None:
            return ScoreResult(
                quality=0.0,
                scorer=self.name,
                details={"patch": "none", "error": found.problem, "expected": expected},
            )
        try:
            outcome = await offload(run_hidden_tests, task, found.text)
        except (SandboxError, OSError) as exc:
            msg = f"the sandbox could not run task '{task.id}': {exc}"
            raise ScoringError(msg) from exc
        return ScoreResult(
            quality=outcome.quality, scorer=self.name, details=outcome.details(expected)
        )
