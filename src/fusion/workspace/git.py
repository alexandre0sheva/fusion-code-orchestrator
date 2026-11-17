"""Read-only git commands for the workspace reader.

Fusion runs git only to read: ``diff``, ``ls-files``, ``check-ignore`` and ``rev-parse``. Every
call turns off what could run a program or write to the repository: external diff drivers and
textconv filters (``--no-ext-diff``, ``--no-textconv``), the fsmonitor hook
(``core.fsmonitor=false``) and opportunistic index refreshes (``GIT_OPTIONAL_LOCKS=0``).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from fusion.security.redaction import redact_error

GIT_TIMEOUT_S = 30.0

# A revision or range such as ``HEAD~1``, ``main...HEAD`` or ``v1.2^{commit}``. No leading dash
# (it would be read as an option), no whitespace, no ``:`` (blob paths).
_REF = re.compile(r"^[A-Za-z0-9_@][A-Za-z0-9_./~^@{}-]*$")

# Variables that would point git at another repository or index than the workspace's own.
_REDIRECTS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_PREFIX")
_SAFE_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.quotePath=false", "-c", "diff.external=")


class GitError(Exception):
    """A git command failed; the message is safe to show (secrets redacted)."""


def valid_ref(ref: str) -> bool:
    """Whether ``ref`` looks like a revision or a ``a..b`` / ``a...b`` range, and nothing else."""
    return bool(ref) and len(ref) <= 200 and _REF.match(ref) is not None


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _REDIRECTS}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", LC_ALL="C")
    return env


def _run(
    root: Path, args: list[str], *, stdin: str | None = None, ok: tuple[int, ...] = (0,)
) -> str:
    try:
        done = subprocess.run(
            ["git", *_SAFE_CONFIG, *args],
            cwd=root,
            env=_env(),
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        msg = "git is not installed or not on PATH"
        raise GitError(msg) from None
    except subprocess.TimeoutExpired:
        msg = f"git {args[0]} took longer than {GIT_TIMEOUT_S:.0f}s"
        raise GitError(msg) from None
    if done.returncode not in ok:
        detail = redact_error(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "")
        msg = f"git {args[0]} failed (exit {done.returncode}): {detail or 'no message'}"
        raise GitError(msg)
    return done.stdout


def is_work_tree(root: Path) -> bool:
    """Whether ``root`` is inside a git working tree."""
    try:
        return _run(root, ["rev-parse", "--is-inside-work-tree"]).strip() == "true"
    except GitError:
        return False


def ignored(root: Path, paths: list[str]) -> set[str]:
    """Those of ``paths`` (relative to ``root``) that ``.gitignore`` rules exclude."""
    if not paths:
        return set()
    out = _run(root, ["check-ignore", "-z", "--stdin"], stdin="\0".join(paths) + "\0", ok=(0, 1))
    return {p for p in out.split("\0") if p}


def listed_files(root: Path) -> list[str]:
    """Tracked and untracked-but-not-ignored files under ``root``, relative to it."""
    out = _run(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    return sorted({p for p in out.split("\0") if p})


def changed_files(root: Path, ref: str) -> list[str]:
    """Files that ``git diff <ref>`` touches under ``root``, relative to it."""
    out = _run(root, ["diff", "--name-only", "-z", "--no-renames", "--relative", ref, "--"])
    return [p for p in out.split("\0") if p]


def diff(root: Path, ref: str, *, exclude: list[str]) -> str:
    """``git diff <ref>`` under ``root`` as unified diff text, leaving out ``exclude``."""
    pathspec = [".", *(f":(exclude,literal){path}" for path in exclude)]
    args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--relative"]
    return _run(root, [*args, ref, "--", *pathspec])
