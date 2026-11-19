"""Workspace reading (roadmap Task 5): what Fusion may read from the user's project, and what not.

Every test builds a throwaway project under ``tmp_path``; nothing touches this repository.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from fusion.security.redaction import is_secret_file
from fusion.workspace import WorkspaceError, WorkspaceReader

FAKE_KEY = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"


def _reader(root: Path, *, total: int = 100_000, per_file: int = 50_000) -> WorkspaceReader:
    return WorkspaceReader(root, max_bytes_total=total, max_bytes_file=per_file)


def _git(root: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid"}
    env |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    # `git commit` starts a detached `git maintenance`, which creates and removes
    # .git/objects/maintenance.lock while _tree_hash walks the tree (a CI race). Turn it off.
    env |= {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "maintenance.auto",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "gc.auto",
        "GIT_CONFIG_VALUE_1": "0",
    }
    done = subprocess.run(
        ["git", *args], cwd=root, env=env, capture_output=True, text=True, check=True
    )
    return done.stdout


def _tree_hash(root: Path) -> str:
    """A hash of every file under ``root`` (``.git`` included), with paths and contents."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(os.readlink(path).encode() if path.is_symlink() else path.read_bytes())
    return digest.hexdigest()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def main():\n    return 1\n")
    (root / "src" / "util.py").write_text("def helper():\n    return 2\n")
    (root / "README.md").write_text("# demo\n")
    return root


@pytest.fixture
def repo(project: Path) -> Path:
    """``project`` as a git repository with two commits and a .gitignore."""
    (project / ".gitignore").write_text("build/\n*.log\n")
    _git(project, "init", "-q", "-b", "main")
    _git(project, "add", ".")
    _git(project, "commit", "-q", "-m", "first")
    (project / "src" / "app.py").write_text("def main():\n    return 42\n")
    _git(project, "commit", "-q", "-am", "second")
    return project


def _reasons(reader: WorkspaceReader) -> dict[str, str]:
    return {item.path: item.reason for item in reader.skipped}


# ----------------------------------------------------------------------------------- happy path


def test_reads_named_files_relative_to_the_root(project: Path) -> None:
    reader = _reader(project)
    snippets = reader.read_paths(["src/app.py", "README.md"])
    assert [s.path for s in snippets] == ["src/app.py", "README.md"]
    assert snippets[0].text == "def main():\n    return 1\n"
    assert snippets[0].as_file_snippet().startswith("src/app.py\n")
    assert reader.skipped == []


def test_an_absolute_path_inside_the_root_is_read(project: Path) -> None:
    snippets = _reader(project).read_paths([str(project / "src" / "app.py")])
    assert [s.path for s in snippets] == ["src/app.py"]


def test_globs_expand_inside_the_root(project: Path) -> None:
    assert _reader(project).expand_globs(["src/*.py"]) == ["src/app.py", "src/util.py"]


def test_a_glob_cannot_climb_out_of_the_root(project: Path) -> None:
    with pytest.raises(WorkspaceError, match="inside the workspace"):
        _reader(project).expand_globs(["../*"])
    with pytest.raises(WorkspaceError, match="inside the workspace"):
        _reader(project).expand_globs(["/etc/*"])


# ---------------------------------------------------------------------------------- containment


def test_parent_and_absolute_paths_outside_the_root_are_refused(project: Path) -> None:
    outside = project.parent / "outside.txt"
    outside.write_text("private")
    reader = _reader(project)
    assert reader.read_paths(["../outside.txt", str(outside), "/etc/hosts"]) == []
    reasons = _reasons(reader)
    assert set(reasons) == {"../outside.txt", str(outside), "/etc/hosts"}
    assert all("outside the workspace" in reason for reason in reasons.values())


def test_a_symlink_that_escapes_the_root_is_refused(project: Path) -> None:
    secret = project.parent / "elsewhere.txt"
    secret.write_text("private")
    (project / "link.txt").symlink_to(secret)
    (project / "linkdir").symlink_to(project.parent)
    reader = _reader(project)
    assert reader.read_paths(["link.txt", "linkdir/elsewhere.txt"]) == []
    assert all("outside the workspace" in r for r in _reasons(reader).values())


def test_a_symlink_inside_the_root_is_followed(project: Path) -> None:
    (project / "alias.py").symlink_to(project / "src" / "app.py")
    assert [s.path for s in _reader(project).read_paths(["alias.py"])] == ["alias.py"]


# ------------------------------------------------------------------------------ deny list, ignore


@pytest.mark.parametrize(
    "name",
    [".env", ".env.local", "server.pem", "id_rsa", "id_ed25519", "tls.key", "secrets.yaml"],
)
def test_secret_files_are_refused_and_never_read(project: Path, name: str) -> None:
    (project / name).write_text(f"TOKEN={FAKE_KEY}\n")
    reader = _reader(project)
    assert reader.read_paths([name]) == []
    assert "secret" in _reasons(reader)[name]


def test_the_deny_list_matches_names_not_ordinary_code() -> None:
    assert is_secret_file("config/.env.production")
    assert is_secret_file("certs/SERVER.PEM")
    assert not is_secret_file("src/id_generator.py")
    assert not is_secret_file("src/keyboard.py")


def test_gitignored_files_are_refused(repo: Path) -> None:
    (repo / "build").mkdir()
    (repo / "build" / "out.js").write_text("compiled")
    (repo / "debug.log").write_text("log")
    reader = _reader(repo)
    assert reader.read_paths(["build/out.js", "debug.log", "src/app.py"])[0].path == "src/app.py"
    assert {k: "gitignore" in v for k, v in _reasons(reader).items()} == {
        "build/out.js": True,
        "debug.log": True,
    }


def test_files_inside_dot_git_are_refused(repo: Path) -> None:
    reader = _reader(repo)
    assert reader.read_paths([".git/config"]) == []
    assert ".git" in _reasons(reader)[".git/config"]


# -------------------------------------------------------------------------- binary, size, budget


def test_binary_files_are_skipped(project: Path) -> None:
    (project / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    reader = _reader(project)
    assert reader.read_paths(["logo.png"]) == []
    assert "binary" in _reasons(reader)["logo.png"]


def test_an_oversize_file_is_skipped_without_being_read(project: Path) -> None:
    big = project / "huge.txt"
    with big.open("wb") as handle:  # a 50 MB sparse file: cheap to make, never read
        handle.truncate(50 * 1024 * 1024)
    reader = _reader(project, per_file=1000)
    assert reader.read_paths(["huge.txt"]) == []
    assert "per-file limit" in _reasons(reader)["huge.txt"]


def test_files_past_the_total_budget_are_skipped(project: Path) -> None:
    (project / "a.txt").write_text("a" * 600)
    (project / "b.txt").write_text("b" * 600)
    reader = _reader(project, total=1000)
    assert [s.path for s in reader.read_paths(["a.txt", "b.txt"])] == ["a.txt"]
    assert "total read limit" in _reasons(reader)["b.txt"]


def test_missing_files_and_directories_are_skipped_with_a_reason(project: Path) -> None:
    reader = _reader(project)
    assert reader.read_paths(["nope.py", "src"]) == []
    reasons = _reasons(reader)
    assert "not found" in reasons["nope.py"]
    assert "directory" in reasons["src"]


def test_the_same_file_twice_is_read_once(project: Path) -> None:
    reader = _reader(project)
    assert len(reader.read_paths(["src/app.py", "./src/app.py"])) == 1


# ----------------------------------------------------------------------------------- redaction


def test_secrets_inside_read_files_are_redacted(project: Path) -> None:
    (project / "settings.py").write_text(f'API_KEY = "{FAKE_KEY}"\n')
    snippet = _reader(project).read_paths(["settings.py"])[0]
    assert FAKE_KEY not in snippet.text
    assert "[REDACTED]" in snippet.text
    assert snippet.redactions == 1


# ------------------------------------------------------------------------------------- git diff


def test_git_diff_reads_a_commit_range_without_changing_the_repo(repo: Path) -> None:
    before = _tree_hash(repo)
    reader = _reader(repo)
    diff = reader.git_diff("HEAD~1")
    assert "-    return 1" in diff and "+    return 42" in diff
    assert _tree_hash(repo) == before


def test_git_diff_leaves_out_secret_files(repo: Path) -> None:
    (repo / ".env").write_text(f"KEY={FAKE_KEY}\n")
    (repo / "src" / "util.py").write_text("def helper():\n    return 3\n")
    _git(repo, "add", "-f", ".env", "src/util.py")
    _git(repo, "commit", "-q", "-m", "third")
    reader = _reader(repo)
    diff = reader.git_diff("HEAD~1")
    assert "util.py" in diff
    assert ".env" not in diff and FAKE_KEY not in diff
    assert "secret" in _reasons(reader)[".env"]


def test_git_diff_redacts_secrets_in_changed_lines(repo: Path) -> None:
    (repo / "src" / "app.py").write_text(f'TOKEN = "{FAKE_KEY}"\n')
    _git(repo, "commit", "-q", "-am", "oops")
    diff = _reader(repo).git_diff("HEAD~1")
    assert FAKE_KEY not in diff and "[REDACTED]" in diff


@pytest.mark.parametrize("ref", ["--output=/tmp/x", "-p", "main; rm -rf /", "a b", ""])
def test_git_diff_refuses_option_like_or_malformed_refs(repo: Path, ref: str) -> None:
    with pytest.raises(WorkspaceError, match="ref"):
        _reader(repo).git_diff(ref)


def test_git_diff_of_an_unknown_ref_says_so(repo: Path) -> None:
    with pytest.raises(WorkspaceError, match="git diff"):
        _reader(repo).git_diff("no-such-branch")


def test_git_diff_outside_a_repository_says_so(project: Path) -> None:
    with pytest.raises(WorkspaceError, match="git diff"):
        _reader(project).git_diff("HEAD~1")


def test_git_diff_over_the_total_budget_is_refused(repo: Path) -> None:
    (repo / "src" / "app.py").write_text("x = 1\n" * 500)
    _git(repo, "commit", "-q", "-am", "big")
    with pytest.raises(WorkspaceError, match="read limit"):
        _reader(repo, total=500).git_diff("HEAD~1")


# --------------------------------------------------------------------------------- the report


def test_the_report_lists_reads_skips_and_the_host_token_saving(repo: Path) -> None:
    (repo / ".env").write_text("X=1\n")
    reader = _reader(repo)
    diff = reader.git_diff("HEAD~1")
    reader.read_paths(["src/util.py", ".env"])
    report = reader.report()
    assert report.diff_ref == "HEAD~1"
    assert report.diff_bytes == len(diff.encode())
    assert [f.path for f in report.read] == ["src/util.py"]
    assert [s.path for s in report.skipped] == [".env"]
    util_bytes = (repo / "src" / "util.py").stat().st_size
    assert report.bytes_read == report.diff_bytes + util_bytes
    assert report.host_tokens_saved_estimate == report.bytes_read // 4


def test_reading_never_changes_the_project(repo: Path) -> None:
    before = _tree_hash(repo)
    reader = _reader(repo)
    reader.read_paths(reader.expand_globs(["**/*.py"]))
    reader.git_diff("HEAD~1")
    assert _tree_hash(repo) == before


def test_git_diff_head_covers_uncommitted_changes(repo: Path) -> None:
    (repo / "src" / "util.py").write_text("def helper():\n    return 99\n")
    assert "+    return 99" in _reader(repo).git_diff("HEAD")
