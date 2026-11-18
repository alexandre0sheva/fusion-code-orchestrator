"""fusion_propose_patch: candidate patches cross-checked in a sandbox copy of the project (or by
the host), one returned as a unified diff, and the project never changed (Part 3, rule 5)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from _coding import ABSOLUTE, BUGGY, FIXED, SQUARED, VISIBLE
from _scripted import FAST, SECURITY, WEAK, Scripted, pipeline
from fusion.bench.patch import PatchError, diff_files
from fusion.bench.sandbox import detect_isolation
from fusion.config.loader import VerifyConfig
from fusion.mcp_server.schemas import ProposePatchInput, SelectPatchInput
from fusion.mcp_server.tools import FusionTools
from fusion.orchestration.aggregate import default_aggregator, resolve_aggregator
from fusion.orchestration.strategy import Mode, load_strategy_book, may_run_code
from fusion.routing.classifier import TaskType
from fusion.verify import propose
from fusion.verify.propose import (
    choose_mode,
    copy_fallback,
    normalize_patch,
    select_from_results,
)
from fusion.workspace import ProjectCopy, ProjectFiles, WorkspaceReader

needs_isolation = pytest.mark.skipif(
    detect_isolation() == "none", reason="no sandbox-exec or unshare on this machine"
)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

FILES = {"calc.py": BUGGY, "tests/test_visible.py": VISIBLE}
GOOD = diff_files(FILES, {**FILES, "calc.py": FIXED})
WRONG = diff_files(FILES, {**FILES, "calc.py": SQUARED})
MALFORMED = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n-def nothing_like_this():\n+x\n"
SECRET = "API_KEY=sk-live-0123456789abcdef0123\n"


# -- a fixture repository -------------------------------------------------------------------------


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text(BUGGY)
    (root / "tests" / "test_visible.py").write_text(VISIBLE)
    (root / ".env").write_text(SECRET)  # not ignored on purpose: the deny list must catch it
    (root / ".gitignore").write_text("build/\n")
    (root / "build").mkdir()
    (root / "build" / "out.txt").write_text("generated\n")
    (root / "logo.bin").write_bytes(b"\xff\x00binary")
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def tree_hash(root: Path) -> str:
    """Every file under ``root`` (``.git`` included), path and bytes: any change shows."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode())
        if path.is_file() and not path.is_symlink():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def chk(name: str, body: str, kind: str = "repro") -> dict[str, str]:
    return {"name": name, "language": "python", "kind": kind, "body": body}


def add_check(name: str, expr: str) -> dict[str, str]:
    return chk(name, f"from calc import add\nassert {expr}\n")


def reply(patch: str | None, checks: Sequence[dict[str, str]] = ()) -> str:
    return json.dumps(
        {
            "summary": "a fix",
            "claims": [],
            "patch": patch,
            "confidence": 0.7,
            "score": None,
            "none_found": False,
            "checks": list(checks),
            "guardrails": [],
        }
    )


def majority_wrong() -> Scripted:
    return Scripted(
        {
            FAST: reply(GOOD, [add_check("sum", "add(2, 3) == 5")]),
            SECURITY: reply(WRONG, [add_check("four", "add(2, 2) == 4")]),
            WEAK: reply(WRONG, [add_check("zero", "add(0, 0) == 0 and add(2, 2) == 4")]),
        }
    )


def tools_for(tmp_path: Path, provider: Scripted) -> FusionTools:
    db = str(tmp_path / "s.db")
    return FusionTools(patch=pipeline(tmp_path, provider), db_path=db, use_mock=True)


def git_apply_check(root: Path, diff: str, tmp_path: Path) -> None:
    """``git apply --check`` of ``diff`` on a throwaway copy of ``root``."""
    scratch = tmp_path / "apply-check"
    shutil.copytree(root, scratch)
    (tmp_path / "fusion.patch").write_text(diff)
    done = subprocess.run(
        ["git", "apply", "--check", str(tmp_path / "fusion.patch")],
        cwd=scratch,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    subprocess.run(["git", "apply", str(tmp_path / "fusion.patch")], cwd=scratch, check=True)
    assert (scratch / "calc.py").read_text() == FIXED


# -- the copy -------------------------------------------------------------------------------------


@needs_git
def test_the_copy_leaves_out_secret_ignored_and_linked_files(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    (root / "link.py").symlink_to(root / "calc.py")
    before = tree_hash(root)
    copy = WorkspaceReader(root).copy_project()
    assert set(copy.files) == {"calc.py", "tests/test_visible.py", ".gitignore", "logo.bin"}
    assert copy.files["logo.bin"].startswith(b"\xff")  # binaries are copied, not sent anywhere
    reasons = {s.path: s.reason for s in copy.skipped}
    assert "secret" in reasons[".env"] and "symlink" in reasons["link.py"]
    assert copy.complete and tree_hash(root) == before


def test_a_copy_over_its_limits_is_incomplete_and_big_files_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / "plain"  # not a git repository: heavy trees are skipped by name
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "i.js").write_text("x")
    for i in range(3):
        (root / f"m{i}.py").write_text("x = 1\n")
    (root / "big.dat").write_bytes(b"a" * 2000)
    copy = WorkspaceReader(root).copy_project(max_file_bytes=1000)
    assert set(copy.files) == {"m0.py", "m1.py", "m2.py"}
    assert any(s.path == "big.dat" and "per-file" in s.reason for s in copy.skipped)
    small = WorkspaceReader(root).copy_project(max_files=2)
    assert not small.complete and "more than 2 files" in small.incomplete_reason


# -- the patch the host gets ----------------------------------------------------------------------


def as_bytes(files: dict[str, str]) -> dict[str, bytes]:
    return {p: c.encode() for p, c in files.items()}


def test_a_blob_or_sloppy_diff_becomes_a_clean_unified_diff() -> None:
    blob = f"=== calc.py ===\n{FIXED}"
    from_blob = normalize_patch(as_bytes(FILES), blob)
    assert from_blob.files == ["calc.py"] and from_blob.diff == GOOD
    sloppy = GOOD.replace("@@ -1,2 +1,2 @@", "@@ -7,9 +7,9 @@")  # wrong numbers, right lines
    assert normalize_patch(as_bytes(FILES), sloppy).diff == GOOD
    created = normalize_patch(as_bytes(FILES), "=== new.py ===\nX = 1\n")
    assert created.diff.startswith("--- /dev/null\n+++ b/new.py\n")


def test_a_file_without_a_final_newline_gets_the_marker() -> None:
    files = {"a.txt": b"one\ntwo"}
    diff = normalize_patch(files, "=== a.txt ===\none\nTWO").diff
    assert diff == (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n one\n-two\n"
        "\\ No newline at end of file\n+TWO\n"
    )


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        (MALFORMED, "does not match|could not|not found|hunk"),
        ("no diff here at all", "no file diffs"),
        (f"=== calc.py ===\n{BUGGY}", "changes nothing"),
        ("=== ../escape.py ===\nx\n", "unsafe path"),
        ("--- a/logo.bin\n+++ b/logo.bin\n@@ -1 +1 @@\n-x\n+y\n", "does not exist|binary"),
    ],
)
def test_a_malformed_or_empty_patch_is_rejected(patch: str, message: str) -> None:
    files = {**as_bytes(FILES), "logo.bin": b"\xff\x00"}
    with pytest.raises(PatchError, match=message):
        normalize_patch(files, patch)


# -- which mode runs ------------------------------------------------------------------------------


def test_sandbox_falls_back_to_host_when_off_unavailable_or_too_big() -> None:
    on, off = VerifyConfig(), VerifyConfig(sandbox_enabled=False)
    assert choose_mode("sandbox", on, isolation="sandbox-exec") == ("sandbox", [])
    mode, why = choose_mode("sandbox", off, isolation="sandbox-exec")
    assert mode == "host" and "sandbox_enabled is false" in why[0]
    mode, why = choose_mode("sandbox", on, isolation="none")
    assert mode == "host" and "No sandbox on this machine" in why[0]
    assert choose_mode("host", on) == ("host", [])
    assert choose_mode("none", off) == ("none", [])
    assert copy_fallback(ProjectCopy(root="/x")) is None
    big = ProjectCopy(root="/x")
    big.stop("more than 5,000 files")
    assert "5,000 files" in (copy_fallback(big) or "")


@needs_git
def test_host_and_none_read_only_what_a_patch_touches(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    files = ProjectFiles(WorkspaceReader(root), max_file_bytes=1_000_000)
    assert normalize_patch(files, GOOD).diff == GOOD
    assert list(files) == ["calc.py"]  # nothing else was read
    assert ".env" not in files and "build/out.txt" not in files  # secret, ignored
    with pytest.raises(PatchError):
        normalize_patch(files, "--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-API_KEY=x\n+API_KEY=y\n")


# -- the strategy may run code here, and only here ------------------------------------------------


def test_auto_picks_verified_synth_for_a_patch_only_when_a_cross_checker_is_given() -> None:
    assert default_aggregator(TaskType.DEFAULT, patch=True) == "select+patch"
    assert default_aggregator(TaskType.DEFAULT, patch=True, verified=True) == "verified-synth"
    book = load_strategy_book()
    panel = resolve_aggregator(
        book.get("panel-nano-auto"), TaskType.DEFAULT, patch=True, verified=True
    )
    assert (panel.aggregator, panel.approaches, panel.lenses) == ("verified-synth", "auto", "off")
    solo = resolve_aggregator(
        book.get("solo-haiku-check"), TaskType.DEFAULT, patch=True, verified=True
    )
    assert not solo.runs_code  # one answer has no rivals to cross-check


def test_only_verified_synth_with_a_cross_checker_runs_code_outside_a_benchmark() -> None:
    book = load_strategy_book()
    synth, verified = book.get("verified-synth"), book.get("best-of-n-verified")
    assert may_run_code(synth, Mode.REAL, crosscheck=True)
    assert not may_run_code(synth, Mode.REAL, crosscheck=False)
    assert not may_run_code(verified, Mode.REAL, crosscheck=True)  # it needs the bench's tests
    assert may_run_code(verified, Mode.BENCHMARK, crosscheck=False)


# -- end to end: sandbox ---------------------------------------------------------------------------


@needs_isolation
@needs_git
async def test_sandbox_returns_the_cross_checked_diff_and_leaves_the_project_alone(
    tmp_path: Path,
) -> None:
    root = make_project(tmp_path)
    before = tree_hash(root)
    tests_cmd = (
        f"{sys.executable} -c \"import os, sys; open('made_by_tests', 'w').close(); "
        "sys.exit(0 if 'fusion-sandbox-' in os.getcwd() and not os.path.exists('.env') "
        "and not os.path.exists('build') else 1)\""
    )
    tools = tools_for(tmp_path, majority_wrong())
    out = await tools.fusion_propose_patch(
        ProposePatchInput(task="Fix add", paths=["calc.py"], tests_cmd=tests_cmd), root=root
    )
    assert out["verify"] == "sandbox" and out["winner"] == FAST, out["warnings"]
    assert out["patch"] == GOOD and out["files"] == ["calc.py"]
    assert out["matrix"][FAST]["task/tests_cmd"] == "pass"  # ran in the copy, saw no .env
    assert out["matrix"][SECURITY][f"{FAST}/sum"] == "fail"
    assert out["isolation"] in {"sandbox-exec", "unshare"}
    assert {c["id"] for c in out["checks"]} >= {"task/tests_cmd", f"{FAST}/sum"}
    assert "git apply" in out["apply"] and "```diff" in out["display_markdown"]
    assert out["host_tokens_saved_estimate"] and out["host_tokens_saved_estimate"] > 0
    assert tree_hash(root) == before  # byte-identical: tests_cmd's file went to the copy
    assert not (root / "made_by_tests").exists()
    git_apply_check(root, out["patch"], tmp_path)
    record = tools.run_record(out["run_id"])
    assert record["output"]["disagreement"]["crosscheck"]["winner"] == FAST


@needs_isolation
@needs_git
async def test_a_failing_tests_cmd_costs_a_patch_its_lead(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    same = [add_check("sum", "add(2, 3) == 5")]
    provider = Scripted(
        {
            FAST: reply(GOOD, same),
            SECURITY: reply(diff_files(FILES, {**FILES, "calc.py": ABSOLUTE}), same),
            WEAK: reply(MALFORMED, same),
        }
    )
    tests_cmd = f'{sys.executable} -c "from calc import add; assert add(-1, 1) == 0"'
    out = await tools_for(tmp_path, provider).fusion_propose_patch(
        ProposePatchInput(task="Fix add", paths=["calc.py"], tests_cmd=tests_cmd), root=root
    )
    assert out["winner"] == FAST and out["patch"] == GOOD
    assert out["matrix"][SECURITY]["task/tests_cmd"] == "fail"
    assert out["matrix"][WEAK]["task/tests_cmd"] == "error"  # its patch does not apply


# -- end to end: host and none --------------------------------------------------------------------


def forbid_running(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ran: list[str] = []

    def boom(*args: Any, **kwargs: Any) -> Any:
        ran.append("ran")
        raise AssertionError("host mode must never run code")

    monkeypatch.setattr("fusion.verify.crosscheck.crosscheck", boom)
    monkeypatch.setattr("fusion.verify.propose.crosscheck", boom)
    monkeypatch.setattr("fusion.bench.sandbox.Sandbox.run", boom)
    return ran


@needs_git
async def test_host_mode_runs_nothing_and_the_hosts_results_pick_the_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = forbid_running(monkeypatch)
    root = make_project(tmp_path)
    before = tree_hash(root)
    tools = tools_for(tmp_path, majority_wrong())
    out = await tools.fusion_propose_patch(
        ProposePatchInput(task="Fix add", paths=["calc.py"], verify="host", tests_cmd="make test"),
        root=root,
    )
    assert out["verify"] == "host" and out.get("patch") is None and not ran
    ids = [c["id"] for c in out["candidates"]]
    assert len(ids) == 2 and set(ids) <= {FAST, SECURITY, WEAK}  # two distinct patches
    assert {c["patch"] for c in out["candidates"]} == {GOOD, WRONG}
    assert {c["id"] for c in out["checks"]} == {f"{FAST}/sum", f"{SECURITY}/four", f"{WEAK}/zero"}
    assert all(c["body"] for c in out["checks"])
    assert (
        "fusion_select_patch" in out["display_markdown"] and "make test" in out["display_markdown"]
    )
    assert tree_hash(root) == before
    good = next(c["id"] for c in out["candidates"] if c["patch"] == GOOD)
    bad = next(c["id"] for c in out["candidates"] if c["patch"] == WRONG)
    checks = [c["id"] for c in out["checks"]]
    picked = await tools.fusion_select_patch(
        SelectPatchInput(
            run_id=out["run_id"],
            results={
                good: dict.fromkeys(checks, "pass") | {"tests": "pass"},
                bad: {
                    f"{FAST}/sum": "fail",
                    f"{SECURITY}/four": "pass",
                    f"{WEAK}/zero": "pass",
                    "tests": "fail",
                },
            },
            baseline={f"{FAST}/sum": "fail", f"{SECURITY}/four": "pass", f"{WEAK}/zero": "fail"},
        )
    )
    assert picked["winner"] == good and picked["patch"] == GOOD
    assert "git apply" in picked["apply"]
    assert f"{SECURITY}/four" in picked["display_markdown"]  # dropped: it already passed


@needs_git
async def test_sandbox_disabled_in_config_means_host_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = forbid_running(monkeypatch)
    monkeypatch.setenv("FUSION__VERIFY__SANDBOX_ENABLED", "false")
    out = await tools_for(tmp_path, majority_wrong()).fusion_propose_patch(
        ProposePatchInput(task="Fix add", paths=["calc.py"]), root=make_project(tmp_path)
    )
    assert out["verify"] == "host" and not ran
    assert any("sandbox_enabled is false" in w for w in out["warnings"])


async def test_no_sandbox_on_this_machine_means_host_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = forbid_running(monkeypatch)
    monkeypatch.setattr(propose, "detect_isolation", lambda: "none")
    out = await tools_for(tmp_path, majority_wrong()).fusion_propose_patch(
        ProposePatchInput(task="Fix add", paths=["calc.py"]), root=make_project(tmp_path)
    )
    assert out["verify"] == "host" and not ran
    assert any("No sandbox on this machine" in w for w in out["warnings"])


async def test_verify_none_rejects_a_malformed_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = forbid_running(monkeypatch)
    provider = Scripted(
        {FAST: reply(MALFORMED), SECURITY: reply(MALFORMED), WEAK: reply(MALFORMED)}
    )
    out = await tools_for(tmp_path, provider).fusion_propose_patch(
        ProposePatchInput(task="Fix add", paths=["calc.py"], verify="none"),
        root=make_project(tmp_path),
    )
    assert out["verify"] == "none" and out.get("patch") is None and not ran
    assert any("rejected" in w for w in out["warnings"])
    assert "No applicable patch" in out["display_markdown"]


async def test_verify_none_returns_the_selected_patch_normalised(tmp_path: Path) -> None:
    provider = Scripted(
        {FAST: reply(f"=== calc.py ===\n{FIXED}"), SECURITY: reply(GOOD), WEAK: reply(GOOD)}
    )
    out = await tools_for(tmp_path, provider).fusion_propose_patch(
        ProposePatchInput(task="Fix add", paths=["calc.py"], verify="none"),
        root=make_project(tmp_path),
    )
    assert out["patch"] == GOOD and out["strategy"] == "panel-nano-select"


def test_the_tool_needs_code_to_change() -> None:
    with pytest.raises(ValueError, match="name the code to change"):
        ProposePatchInput(task="Fix add")


# -- host results -> a pick -----------------------------------------------------------------------


def handoff_record() -> dict[str, Any]:
    return {
        "groups": {"a": "a", "b": "b", "c": "c"},
        "handoff": {
            "candidates": [
                {"id": "a", "group": "a", "patch": "A", "error": None},
                {"id": "b", "group": "b", "patch": "B", "error": None},
                {"id": "c", "group": "c", "patch": None, "error": "does not apply"},
            ],
            "checks": [
                {"id": "a/x", "author": "a", "kind": "repro", "language": "python", "body": "1"},
                {"id": "b/y", "author": "b", "kind": "repro", "language": "python", "body": "1"},
                {
                    "id": "b/inv",
                    "author": "b",
                    "kind": "invariant",
                    "language": "python",
                    "body": "1",
                },
            ],
        },
    }


def test_the_hosts_results_are_scored_like_the_sandbox() -> None:
    record = handoff_record()
    chosen = select_from_results(
        record,
        {"a": {"a/x": "pass", "b/y": "pass"}, "b": {"a/x": "fail", "b/y": "pass"}},
    )
    assert chosen.winner == "a" and chosen.patch == "A"
    assert chosen.scores == {"a": 1.5, "b": 0.5}  # own check counts half
    # the baseline drops a repro that already passes and an invariant that already fails
    dropped = select_from_results(
        record,
        {"a": {"a/x": "pass", "b/inv": "pass"}, "b": {"a/x": "pass", "b/inv": "pass"}},
        baseline={"a/x": "pass", "b/inv": "fail"},
    )
    assert set(dropped.dropped) == {"a/x", "b/inv"} and dropped.tied == ["a", "b"]
    assert dropped.winner == "a" and "first of them" in dropped.reason
    own = select_from_results(record, {"a": {"tests": "fail"}, "b": {"tests": "pass"}})
    assert own.winner == "b" and own.host_checks == ["tests"]
    assert select_from_results(record, {"c": {"a/x": "pass"}}).patch is None  # c cannot win


@pytest.mark.parametrize(
    ("record", "results", "message"),
    [
        ({}, {"a": {}}, "not a host-mode"),
        (handoff_record(), {"zz": {"a/x": "pass"}}, "unknown candidate"),
        (handoff_record(), {"a": {"a/x": "PASSED"}}, "pass, fail, error or timeout"),
    ],
)
def test_bad_host_results_are_refused(
    record: dict[str, Any], results: dict[str, dict[str, str]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        select_from_results(record, results)


async def test_selecting_from_an_unknown_run_says_so(tmp_path: Path) -> None:
    tools = tools_for(tmp_path, majority_wrong())
    with pytest.raises(ValueError, match="No run 'nope'"):
        await tools.fusion_select_patch(SelectPatchInput(run_id="nope", results={"a": {}}))


# -- the command line -----------------------------------------------------------------------------


def test_fusion_patch_needs_the_code_to_change() -> None:
    from typer.testing import CliRunner

    from fusion.cli.main import app

    result = CliRunner().invoke(app, ["patch", "Fix add", "--mock"])
    assert result.exit_code == 2 and "--file" in result.output


@needs_git
def test_fusion_patch_json_is_the_tool_record_and_diff_only_exits_3_without_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jsonschema
    from typer.testing import CliRunner

    from fusion.cli.main import app
    from fusion.mcp_server.schemas import PatchToolResult

    root = make_project(tmp_path)
    monkeypatch.chdir(root)
    before = tree_hash(root)
    db = str(tmp_path / "cli.db")
    args = ["patch", "Fix add", "-f", "calc.py", "--verify", "none", "--mock", "--db-path", db]
    result = CliRunner().invoke(app, [*args, "--json"])
    assert result.exit_code == 0, result.output
    record = json.loads(result.stdout)
    jsonschema.validate(record, PatchToolResult.model_json_schema())
    assert record["verify"] == "none"
    only = CliRunner().invoke(app, [*args, "--diff-only"])
    assert only.exit_code == 3 and only.stdout == ""  # the mock models write no patch
    assert tree_hash(root) == before
