"""The cross-check: every candidate patch against every validated, discriminating check, in sandbox
copies, and the pick (checks written by OTHER candidates count in full, a candidate's own count
half: CodeT-style dual agreement). Covers Review-focus item 5 and Part 3, rule 5."""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

import pytest

from fusion.bench.sandbox import detect_isolation
from fusion.verify.crosscheck import (
    CheckEnv,
    Matrix,
    SandboxCopy,
    crosscheck,
    pick,
    run_check,
)
from fusion.verify.synthesis import TASK_AUTHOR, Candidate, CheckScript

needs_isolation = pytest.mark.skipif(
    detect_isolation() == "none", reason="no sandbox-exec or unshare on this machine"
)

BUGGY = "def is_leap(y):\n    return y % 4 == 0\n"
RIGHT = "def is_leap(y):\n    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)\n"
WRONG = "def is_leap(y):\n    return y % 4 == 0 and y % 100 != 0\n"  # forgets the 400 rule
FILES = {"cal.py": BUGGY}
DIFF_WRONG = (
    "--- a/cal.py\n+++ b/cal.py\n@@ -1,2 +1,2 @@\n def is_leap(y):\n"
    "-    return y % 4 == 0\n+    return y % 4 == 0 and y % 100 != 0\n"
)


def blob(source: str) -> str:
    return f"=== cal.py ===\n{source}"


def check(name: str, body: str, kind: str = "repro", language: str = "python") -> CheckScript:
    return CheckScript(name=name, language=language, kind=kind, body=body)  # type: ignore[arg-type]


def leap(expr: str) -> str:
    return f"from cal import is_leap\nassert {expr}\n"


def env(**kw: object) -> CheckEnv:
    return CheckEnv(source=FILES, timeout_s=10, **kw)  # type: ignore[arg-type]


def majority_wrong() -> list[Candidate]:
    return [
        Candidate(
            "a", blob(RIGHT), [check("century", leap("not is_leap(1900) and is_leap(2000)"))]
        ),
        Candidate("b", blob(WRONG), [check("y1900", leap("not is_leap(1900)"))]),
        Candidate("c", DIFF_WRONG, [check("y2100", leap("not is_leap(2100)"))]),
    ]


# -- the copy -------------------------------------------------------------------------------------


def test_a_sandbox_copy_holds_the_patched_files_and_is_deleted_on_exit() -> None:
    with SandboxCopy(FILES, patch=blob(RIGHT)) as box:
        root = box.root
        assert box.read("cal.py").decode() == RIGHT
    assert not root.exists()


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_the_source_tree_is_byte_identical_after_a_run(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (project / "cal.py").write_text(BUGGY)
    before = _tree_hash(project)
    candidates = majority_wrong()
    candidates[0].checks.append(
        check("scribble", "open('cal.py', 'w').write('broken')\nraise SystemExit(1)\n")
    )
    report = crosscheck(candidates, CheckEnv(source=project, timeout_s=10))
    assert report.winner == "a"
    assert _tree_hash(project) == before


# -- the pick -------------------------------------------------------------------------------------


def test_the_cross_check_picks_the_right_patch_when_the_majority_is_wrong() -> None:
    report = crosscheck(majority_wrong(), env())
    assert report.winner == "a"
    assert report.groups["b"] == report.groups["c"] != report.groups["a"]  # same files, one group
    cells = report.matrix.cells
    assert cells["a"] == {"a/century": "pass", "b/y1900": "pass", "c/y2100": "pass"}
    assert cells["b"] == cells["c"] == {"a/century": "fail", "b/y1900": "pass", "c/y2100": "pass"}
    assert report.scores["a"] == pytest.approx(2.5)  # its own check half, two others' in full
    assert report.scores["b"] == pytest.approx(1.0)  # b and c wrote both its checks: half each
    assert "a" in report.reason


def test_a_check_that_already_passes_is_discarded() -> None:
    candidates = majority_wrong()
    candidates[1].checks.append(check("no_signal", leap("is_leap(2004)")))
    report = crosscheck(candidates, env())
    status = {c.id: c.status for c in report.checks}
    assert status["b/no_signal"] == "not_discriminating"
    assert "b/no_signal" not in report.matrix.cells["a"]


def test_an_invariant_must_pass_before_and_is_kept() -> None:
    candidates = majority_wrong()
    candidates[0].checks += [
        check("still_leap", leap("is_leap(2004)"), kind="invariant"),
        check("false_belief", leap("not is_leap(2004)"), kind="invariant"),
    ]
    report = crosscheck(candidates, env())
    status = {c.id: c.status for c in report.checks}
    assert status["a/still_leap"] == "kept"
    assert status["a/false_belief"] == "broken"  # an invariant the code already breaks


def test_an_invalid_check_is_dropped_before_any_run() -> None:
    candidates = majority_wrong()
    candidates[2].checks.append(check("phone_home", "import socket\n"))
    report = crosscheck(candidates, env())
    dropped = next(c for c in report.checks if c.id == "c/phone_home")
    assert dropped.status == "invalid" and any("network" in r for r in dropped.reasons)
    assert dropped.baseline is None  # never ran


def test_a_patch_that_does_not_apply_scores_errors_and_cannot_win() -> None:
    candidates = majority_wrong()
    candidates[0].patch = "--- a/missing.py\n+++ b/missing.py\n@@ -1 +1 @@\n-x\n+y\n"
    report = crosscheck(candidates, env())
    assert set(report.matrix.cells["a"].values()) == {"error"}
    assert report.patch_errors["a"]
    assert report.winner in {"b", "c"}


def test_a_check_that_loops_forever_times_out_and_never_passes() -> None:
    candidates = majority_wrong()
    candidates[1].checks.append(check("spin", "while True:\n    pass\n"))
    report = crosscheck(candidates, CheckEnv(source=FILES, timeout_s=1))
    assert {report.matrix.cells[c]["b/spin"] for c in "abc"} == {"timeout"}
    assert report.winner == "a"


def test_identical_scores_are_a_tie_for_the_selector() -> None:
    candidates = [
        Candidate("a", blob(RIGHT), [check("y1900", leap("not is_leap(1900)"))]),
        Candidate("b", blob(WRONG), [check("y2100", leap("not is_leap(2100)"))]),
    ]
    report = crosscheck(candidates, env())
    assert report.winner is None and sorted(report.tied) == ["a", "b"]


def test_task_checks_count_in_full_and_are_never_discarded() -> None:
    candidates = [
        Candidate("a", blob(RIGHT), []),
        Candidate("b", blob("def is_leap(y):\n    raise RuntimeError\n"), []),
    ]
    visible = check("visible", leap("is_leap(2004)"), kind="invariant")
    report = crosscheck(candidates, env(task_checks=(visible,)))
    assert report.matrix.cells["a"][f"{TASK_AUTHOR}/visible"] == "pass"
    assert report.matrix.cells["b"][f"{TASK_AUTHOR}/visible"] == "fail"
    assert report.winner == "a" and report.scores["a"] == pytest.approx(1.0)


def test_shares_say_how_much_of_the_kept_checks_each_candidate_passed() -> None:
    report = crosscheck(majority_wrong(), env())
    assert report.shares == {
        "a": pytest.approx(1.0),
        "b": pytest.approx(2 / 3),
        "c": pytest.approx(2 / 3),
    }


def test_the_report_is_a_json_record_with_the_matrix_and_the_reason() -> None:
    record = crosscheck(majority_wrong(), env()).record()
    again = json.loads(json.dumps(record))
    assert again["winner"] == "a" and again["matrix"]["b"]["a/century"] == "fail"
    assert again["reason"] and again["checks"][0]["status"] == "kept"


def test_pick_on_a_hand_made_matrix() -> None:
    matrix = Matrix(
        cells={
            "x": {"x/1": "pass", "y/1": "fail", "task/t": "pass"},
            "y": {"x/1": "fail", "y/1": "pass", "task/t": "timeout"},
        },
        authors={"x/1": "x", "y/1": "y", "task/t": TASK_AUTHOR},
        groups={"x": "x", "y": "y"},
    )
    chosen = pick(matrix)
    assert chosen.winner == "x" and chosen.scores == {"x": 1.5, "y": 0.5}


# -- the sandbox around one check (Review focus 5) ------------------------------------------------


def test_run_check_reports_pass_and_fail() -> None:
    with SandboxCopy(FILES) as box:
        assert run_check(box, check("ok", leap("is_leap(2004)")), timeout_s=10) == "pass"
        assert run_check(box, check("no", leap("is_leap(2003)")), timeout_s=10) == "fail"
        shell = check("sh", "grep -q 'y % 4' cal.py\n", language="shell")
        assert run_check(box, shell, timeout_s=10) == "pass"


@needs_isolation
def test_a_check_that_opens_a_socket_fails() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    body = f"import socket\nsocket.create_connection(('127.0.0.1', {port}), timeout=2).close()\n"
    try:
        with SandboxCopy(FILES, isolation="require") as box:
            assert run_check(box, check("net", body), timeout_s=10) == "fail"
    finally:
        listener.close()


@needs_isolation
def test_a_check_that_writes_outside_its_copy_is_blocked(tmp_path: Path) -> None:
    target = tmp_path / "escaped.txt"
    hidden = str(target).encode().hex()  # out of the validator's sight: the sandbox must stop it
    body = f"open(bytes.fromhex('{hidden}').decode(), 'w').write('x')\n"
    with SandboxCopy(FILES, isolation="require") as box:
        assert run_check(box, check("escape", body), timeout_s=10) == "fail"
    assert not target.exists()
