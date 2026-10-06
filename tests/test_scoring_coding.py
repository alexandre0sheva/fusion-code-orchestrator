"""``CodingScorer``: hidden tests in a sandbox, pass@1, the flake guard, and the verifier."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import fusion.bench.scoring.coding as coding
from _coding import ABSOLUTE, EXPECTED, FILES, FIXED, SQUARED, coding_task, patch_for
from fusion.bench.patch import patch_as_blobs
from fusion.bench.scoring import (
    PASS_THRESHOLDS,
    SCORERS,
    AnswerView,
    ScoreEnv,
    ScoringError,
    get_scorer,
    is_solved,
)
from fusion.bench.scoring.coding import (
    CodingScorer,
    coding_verifier,
    parse_test_results,
    run_hidden_tests,
    visible_pass_fraction,
)
from fusion.bench.virtual import run_virtual
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.telemetry.cost import PricingRegistry


def env() -> ScoreEnv:
    gateway = CallGateway(
        ledger=RunLedger(lambda: 0.0),
        models={},
        providers={},
        pricing=PricingRegistry(),
        truncate_prompts=False,
    )
    return ScoreEnv(gateway=gateway)


def score(answer: str, task: Any = None, **view: Any) -> Any:
    task = task or coding_task()
    return asyncio.run(CodingScorer().score(task, AnswerView(final_answer=answer, **view), env()))


def fenced(patch: str) -> str:
    return f"Here is my fix.\n```diff\n{patch}```\n"


@pytest.fixture(autouse=True)
def fresh_cache() -> None:
    coding._outcomes.clear()


# -- reading test output ---------------------------------------------------------------------------


def test_unittest_output_is_read() -> None:
    out = (
        "test_a (tests.test_x.T.test_a) ... ok\n"
        "test_b (tests.test_x.T.test_b) ... FAIL\n"
        "test_c (tests.test_x.T.test_c) ... ERROR\n"
        "test_d (tests.test_x.T.test_d) ... skipped 'later'\n"
        "test_e (tests.test_x.T.test_e)\nA docstring line ... ok\n"
        "ERROR: tests.test_y (unittest.loader._FailedTest.tests.test_y)\n"
    )
    assert parse_test_results(out) == {
        "tests.test_x.T.test_a": "passed",
        "tests.test_x.T.test_b": "failed",
        "tests.test_x.T.test_c": "error",
        "tests.test_x.T.test_d": "skipped",
        "tests.test_x.T.test_e": "passed",
    }


def test_pytest_output_is_read() -> None:
    out = (
        "tests/test_x.py::test_a PASSED [ 50%]\ntests/test_x.py::test_b FAILED [100%]\n"
        "PASSED tests/test_x.py::test_a\n"
    )
    assert parse_test_results(out) == {
        "tests/test_x.py::test_a": "passed",
        "tests/test_x.py::test_b": "failed",
    }


# -- scoring a patch -------------------------------------------------------------------------------


def test_a_correct_patch_passes_every_hidden_test() -> None:
    result = score(fenced(patch_for(FIXED)))
    assert result.quality == 1.0 and result.scorer == "tests" and result.cost_usd == 0.0
    assert result.details["patch"] == "applied"
    assert result.details["passed"] == result.details["expected"] == 3
    assert result.details["runs"] == 1  # nothing failed, so nothing was repeated
    assert "failed" not in result.details


def test_the_patch_may_be_file_blobs_or_the_field_of_a_json_answer() -> None:
    blobs = patch_as_blobs(FILES, patch_for(FIXED))
    assert score(blobs).quality == 1.0
    assert score(json.dumps({"summary": "fixed", "claims": [], "patch": blobs})).quality == 1.0
    assert score("irrelevant text", structured={"patch": patch_for(FIXED)}).quality == 1.0


def test_a_flawed_patch_gets_the_share_of_tests_it_passes() -> None:
    squared = score(fenced(patch_for(SQUARED)))  # 2 * 2 == 2 + 2, so only that case passes
    assert squared.quality == pytest.approx(0.0)
    absolute = score(fenced(patch_for(ABSOLUTE)))  # wrong only for the negative number
    assert absolute.quality == pytest.approx(2 / 3)
    assert absolute.details["failed"] == ["tests.test_hidden.HiddenTests.test_negative"]
    assert not is_solved("coding", absolute.quality)


def test_no_patch_scores_zero_and_says_why() -> None:
    result = score("You should change add to return a + b.")
    assert result.quality == 0.0
    assert result.details["patch"] == "none" and "no patch" in result.details["error"]


def test_several_different_patches_score_zero() -> None:
    both = fenced(patch_for(FIXED)) + fenced(patch_for(SQUARED))
    result = score(both)
    assert result.quality == 0.0 and "2 different patches" in result.details["error"]


def test_a_patch_that_does_not_apply_scores_zero_with_the_reason() -> None:
    bad = (
        "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def nothing():\n-    pass\n+    return 1\n"
    )
    result = score(fenced(bad))
    assert result.quality == 0.0 and result.details["patch"] == "rejected"
    assert "hunk 1 does not apply" in result.details["error"]


def test_a_patch_with_an_unsafe_path_is_rejected_and_writes_nothing(tmp_path: Any) -> None:
    target = tmp_path / "pwned.py"
    result = score(f"=== ../../../../{target} ===\nx = 1\n")
    assert result.quality == 0.0 and "unsafe path" in result.details["error"]
    assert not target.exists()


def test_a_patch_cannot_weaken_the_hidden_tests() -> None:
    hollow = (
        "=== tests/test_hidden.py ===\nimport unittest\n\n\nclass HiddenTests(unittest.TestCase):\n"
        "    def test_add(self):\n        pass\n\n    def test_negative(self):\n        pass\n\n"
        "    def test_big(self):\n        pass\n"
    )
    result = score(hollow)  # no fix at all, and tests rewritten to pass
    assert result.quality == 0.0 and result.details["patch"] == "applied"


def test_a_patch_that_hangs_times_out_with_partial_credit_and_is_not_repeated() -> None:
    task = coding_task(timeout_s=2)
    hang = (
        "def add(a, b):\n    if a == 10:\n        while True:\n            pass\n    return a + b\n"
    )
    result = score(fenced(patch_for(hang)), task)
    assert result.details["timed_out"] is True
    assert 0 < result.quality < 1
    assert result.details["runs"] == 1  # a hang is not a flake, so it is not tried again
    assert result.details["seconds"] < 15


def test_the_sandbox_hides_the_environment_from_the_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-this-must-not-leak")
    leak = (
        "import os\n\n\ndef add(a, b):\n"
        "    assert 'OPENAI_API_KEY' not in os.environ\n    return a + b\n"
    )
    assert score(fenced(patch_for(leak))).quality == 1.0


# -- the flake guard and the cache -----------------------------------------------------------------


class Scripted:
    """Stands in for ``_run_once``: each call returns the next scripted unittest output."""

    def __init__(self, *outputs: dict[str, str]) -> None:
        self.outputs = list(outputs)
        self.calls = 0

    def __call__(self, task: Any, truth: Any, patch: Any, *, hidden: bool) -> Any:
        from fusion.bench.sandbox import RunResult

        self.calls += 1
        results = self.outputs[min(self.calls - 1, len(self.outputs) - 1)]
        text = "".join(f"{i.rsplit('.', 1)[1]} ({i}) ... {s}\n" for i, s in results.items())
        return "", RunResult(0, "", text, 0.1), "none"


def outcome(**status: str) -> dict[str, str]:
    return {i: status.get(i.rsplit(".", 1)[1], "ok") for i in EXPECTED}


def test_a_first_run_that_passes_everything_is_not_repeated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Scripted(outcome())
    monkeypatch.setattr(coding, "_run_once", run)
    result = run_hidden_tests(coding_task(), "patch")
    assert (result.quality, result.runs, run.calls) == (1.0, 1, 1)


def test_a_failure_is_rerun_twice_and_a_test_that_then_passes_is_flaky_but_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Scripted(outcome(test_big="FAIL"), outcome(), outcome())
    monkeypatch.setattr(coding, "_run_once", run)
    result = run_hidden_tests(coding_task(), "patch")
    assert (result.runs, run.calls) == (3, 3)
    assert result.quality == 1.0  # it passed in the majority of runs
    assert result.flaky == ["tests.test_hidden.HiddenTests.test_big"]


def test_a_failure_that_repeats_counts_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Scripted(outcome(test_big="FAIL"), outcome(test_big="FAIL"), outcome())
    monkeypatch.setattr(coding, "_run_once", run)
    result = run_hidden_tests(coding_task(), "patch")
    assert result.quality == pytest.approx(2 / 3) and result.failed == [
        "tests.test_hidden.HiddenTests.test_big"
    ]
    assert result.runs == 3 and result.flaky  # it did pass once, so it is listed as flaky


def test_the_same_patch_is_run_once_and_whitespace_does_not_matter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Scripted(outcome())
    monkeypatch.setattr(coding, "_run_once", run)
    task = coding_task()
    first = run_hidden_tests(task, "a patch  \n")
    assert run_hidden_tests(task, "a patch\n\n") is first and run.calls == 1
    run_hidden_tests(task, "a different patch")
    assert run.calls == 2
    run_hidden_tests(task, "a patch", cache=False)
    assert run.calls == 3


def test_a_timed_out_result_is_not_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    from fusion.bench.sandbox import RunResult

    calls = []

    def hang(task: Any, truth: Any, patch: Any, *, hidden: bool) -> Any:
        calls.append(1)
        return "", RunResult(None, "", "", 1.0, timed_out=True), "none"

    monkeypatch.setattr(coding, "_run_once", hang)
    task = coding_task()
    assert run_hidden_tests(task, "p").timed_out
    run_hidden_tests(task, "p")
    assert len(calls) == 2


# -- where it is registered ------------------------------------------------------------------------


def test_coding_is_scored_by_tests_and_solved_means_every_test_passes() -> None:
    assert isinstance(get_scorer("coding"), CodingScorer) and SCORERS["coding"] is get_scorer(
        "coding"
    )
    assert PASS_THRESHOLDS["coding"] == 1.0
    assert is_solved("coding", 1.0) and not is_solved("coding", 0.99)
    assert CodingScorer().estimate_calls(coding_task(), ["any-judge"]) == []


def test_a_coding_task_with_only_points_truth_falls_back_to_points() -> None:
    from fusion.bench.spec import BenchTask

    task = BenchTask.model_validate(
        {
            "id": "p",
            "category": "coding",
            "prompt": "Explain the fix.",
            "truth": {"points": [{"id": "a", "keywords": ["off-by-one"]}]},
        }
    )
    assert score("It is an off-by-one error.", task).quality == 1.0
    empty = BenchTask.model_validate(
        {"id": "q", "category": "coding", "prompt": "x", "truth": {"nothing": 1}}
    )
    with pytest.raises(ScoringError, match="cannot score"):
        score("x", empty)


def test_scoring_works_on_the_virtual_time_loop() -> None:
    async def go() -> float:
        return (
            await CodingScorer().score(
                coding_task(), AnswerView(final_answer=fenced(patch_for(FIXED))), env()
            )
        ).quality

    assert run_virtual(go()) == 1.0


# -- the verifier (visible tests) ------------------------------------------------------------------


def test_the_visible_tests_are_what_the_verifier_runs() -> None:
    task = coding_task()
    assert visible_pass_fraction(task, patch_for(FIXED)) == 1.0
    assert (
        visible_pass_fraction(task, patch_for(SQUARED)) == 1.0
    )  # visible tests are weaker: it passes
    assert visible_pass_fraction(task, patch_for("def add(a, b):\n    return 0\n")) == 0.0
    assert visible_pass_fraction(task, None) == 0.0  # the unpatched files fail their visible test


def test_a_patch_that_does_not_apply_is_minus_one_and_no_visible_tests_is_none() -> None:
    task = coding_task()
    assert visible_pass_fraction(task, "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-nope\n+x\n") == -1.0
    bare = task.model_copy(update={"files": {"calc.py": task.files["calc.py"]}})
    assert visible_pass_fraction(bare, patch_for(FIXED)) is None


def test_the_verifier_is_only_for_coding_tasks() -> None:
    verify = coding_verifier(coding_task())
    assert verify is not None
    assert asyncio.run(verify(patch_for(FIXED))) == 1.0
    assert run_virtual(verify(patch_for(FIXED))) == 1.0
    from fusion.bench.spec import BenchTask

    other = BenchTask.model_validate(
        {"id": "r", "category": "code_review", "prompt": "x", "truth": {"bugs": []}}
    )
    assert coding_verifier(other) is None
