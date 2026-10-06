"""Executable coding tasks, stored as directories and read as ``BenchTask`` rows.

One task is one directory under ``evals/datasets/v1/coding/``::

    <task_id>/
      task.yaml            id, difficulty, split, tags, expected_pass, solution_summary, flaws
      prompt.md            what the model is asked to do (the patch format request is appended)
      repo/                the files the model sees: the code, and any *visible* tests
      tests/               the HIDDEN tests: copied in after the model's patch, never shown to it
      reference/solution/  the files a correct fix changes (their new content)
      reference/flaw-N/    the files of the N-th plausible-but-wrong fix, in ``task.yaml`` order

The compiled row's ``truth`` is what ``CodingTruth`` (``fusion.bench.spec``) describes: the test
command, the hidden files, ``expected_pass`` (the ids of the hidden tests a correct patch passes),
and the reference solution and flaws as unified diffs. Those two exist for the validator (it proves
the tests tell a right fix from a wrong one) and for simulated models; no real model ever sees
the truth. A directory is a task when it holds ``task.yaml``; nothing below it is read as dataset
rows, so a task's own YAML or JSONL files are just files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from fusion.bench.patch import diff_files

__all__ = [
    "DEFAULT_TEST_COMMAND",
    "PATCH_REQUEST",
    "TASK_FILE",
    "CodingTaskError",
    "is_task_dir",
    "load_task_dir",
]

TASK_FILE = "task.yaml"
DEFAULT_TEST_COMMAND = ("python", "-m", "unittest", "discover", "-v", "-s", "tests", "-t", ".")
PATCH_REQUEST = (
    "Answer with ONE patch against the files shown: either a unified diff (`--- a/path`, "
    "`+++ b/path`, `@@` hunks) or the complete new content of every file you change, each "
    "introduced by a line `=== path/to/file.py ===`. Put the patch in the `patch` field of your "
    "JSON answer and nothing else in it (the claims describe what you changed). Do not edit or "
    "delete the tests. Python 3.12, standard library only."
)
_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}
_SKIP_NAMES = {".DS_Store"}


class CodingTaskError(ValueError):
    """A task directory that cannot be read."""


def is_task_dir(path: Path) -> bool:
    return path.is_dir() and (path / TASK_FILE).is_file()


def _read_tree(root: Path) -> dict[str, str]:
    """Every text file under ``root`` as ``{relative posix path: content}``."""
    tree: dict[str, str] = {}
    for item in sorted(root.rglob("*")):
        rel = item.relative_to(root)
        if (
            not item.is_file()
            or item.is_symlink()
            or item.suffix in _SKIP_SUFFIXES
            or item.name in _SKIP_NAMES
            or any(part in _SKIP_DIRS for part in rel.parts)
        ):
            continue
        try:
            tree[rel.as_posix()] = item.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            msg = f"{item}: not valid UTF-8 text"
            raise CodingTaskError(msg) from exc
    return tree


def _overlay_diff(files: dict[str, str], folder: Path) -> str:
    """The unified diff taking ``files`` to the same tree with ``folder``'s files laid over it."""
    if not folder.is_dir():
        return ""
    after = {**files, **_read_tree(folder)}
    return diff_files(files, after)


def load_task_dir(path: Path) -> dict[str, Any]:
    """The ``BenchTask`` row (a dict) for the task directory ``path``."""
    try:
        manifest = yaml.safe_load((path / TASK_FILE).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        msg = f"{path / TASK_FILE}: not valid YAML ({exc})"
        raise CodingTaskError(msg) from exc
    if not isinstance(manifest, dict):
        msg = f"{path / TASK_FILE} must hold a mapping"
        raise CodingTaskError(msg)
    prompt_file = path / "prompt.md"
    if not prompt_file.is_file():
        msg = f"{path}: missing prompt.md"
        raise CodingTaskError(msg)
    repo = path / "repo"
    if not repo.is_dir():
        msg = f"{path}: missing repo/"
        raise CodingTaskError(msg)
    files = _read_tree(repo)
    hidden = {f"tests/{rel}": text for rel, text in _read_tree(path / "tests").items()}
    reference = path / "reference"
    flaws = [
        {
            "summary": str(flaw.get("summary", "")) if isinstance(flaw, dict) else str(flaw),
            "patch": _overlay_diff(files, reference / f"flaw-{n}"),
        }
        for n, flaw in enumerate(manifest.get("flaws") or [], 1)
    ]
    truth: dict[str, Any] = {
        "command": list(manifest.get("command") or DEFAULT_TEST_COMMAND),
        "timeout_s": manifest.get("timeout_s", 30),
        "mem_mb": manifest.get("mem_mb", 1024),
        "expected_pass": list(manifest.get("expected_pass") or []),
        "hidden_files": hidden,
        "solution": _overlay_diff(files, reference / "solution"),
        "solution_summary": str(manifest.get("solution_summary", "")),
        "flaws": flaws,
    }
    tags = [*(manifest.get("tags") or []), "synthetic", "llm-authored"]
    row: dict[str, Any] = {
        "id": str(manifest.get("id") or path.name),
        "category": "coding",
        "difficulty": manifest.get("difficulty", "medium"),
        "prompt": f"{prompt_file.read_text(encoding='utf-8').strip()}\n\n{PATCH_REQUEST}",
        "files": files,
        "truth": truth,
        "tags": list(dict.fromkeys(tags)),
    }
    if manifest.get("split"):
        row["split"] = manifest["split"]
    return row
