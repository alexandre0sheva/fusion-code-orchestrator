"""Executable coding tasks, stored as directories and read as ``BenchTask`` rows.

One task is one directory under ``evals/datasets/v1/coding/``, ``frontend/`` or ``performance/``::

    <task_id>/
      task.yaml            id, category, difficulty, split, tags, expected_pass, solution_summary,
                           flaws and, for frontend and performance tasks, the keys below
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

``category`` in ``task.yaml`` is ``coding`` (the default), ``frontend`` or ``performance``. The
last two are scored on more than their hidden tests (``fusion.bench.scoring.artifact``) and add
``hard_gates``, ``soft_criteria``, ``evaluators``, ``perf`` and ``site`` (``ArtifactTruth``),
copied into the row's ``truth`` as written. ``helpers: [sitecheck]`` copies a shared test helper
from ``fusion/bench/datasets/helpers`` into the hidden tests, so each task's tests need not repeat
it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml

from fusion.bench.patch import diff_files

__all__ = [
    "DEFAULT_TEST_COMMAND",
    "HELPERS_DIR",
    "PATCH_REQUEST",
    "TASK_FILE",
    "CodingTaskError",
    "is_task_dir",
    "load_task_dir",
    "patch_request",
]

TASK_FILE = "task.yaml"
DEFAULT_TEST_COMMAND = ("python", "-m", "unittest", "discover", "-v", "-s", "tests", "-t", ".")
_PATCH_FORMAT = (  # ``%s``: an example file name
    "Answer with ONE patch against the files shown: either a unified diff (`--- a/path`, "
    "`+++ b/path`, `@@` hunks) or the complete new content of every file you change, each "
    "introduced by a line `=== path/to/%s ===`. Put the patch in the `patch` field of your "
    "JSON answer and nothing else in it (the claims describe what you changed). Do not edit or "
    "delete the tests. "
)
_PYTHON_ONLY = "Python 3.12, standard library only."
_FRONTEND_ONLY = (
    "Plain HTML, CSS and JavaScript only: no frameworks, no build step, and no network (no CDN "
    "scripts, fonts or images; inline or local files only)."
)
PATCH_REQUEST = _PATCH_FORMAT % "file.py" + _PYTHON_ONLY
HELPERS_DIR: Final = Path(__file__).parent / "helpers"
ARTIFACT_MANIFEST_KEYS = ("evaluators", "hard_gates", "soft_criteria", "perf", "site")
CATEGORIES = ("coding", "frontend", "performance")
_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}
_SKIP_NAMES = {".DS_Store"}


class CodingTaskError(ValueError):
    """A task directory that cannot be read."""


def patch_request(category: str) -> str:
    """The closing paragraph of a prompt: how to answer, and what the code may use."""
    if category == "frontend":
        return _PATCH_FORMAT % "file.html" + _FRONTEND_ONLY
    return PATCH_REQUEST


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
    for helper in manifest.get("helpers") or []:
        source = HELPERS_DIR / f"{helper}.py"
        if not source.is_file():
            msg = f"{path / TASK_FILE}: unknown helper '{helper}'"
            raise CodingTaskError(msg)
        hidden.setdefault(f"tests/{helper}.py", source.read_text(encoding="utf-8"))
    category = str(manifest.get("category") or "coding")
    if category not in CATEGORIES:
        msg = f"{path / TASK_FILE}: category must be one of {', '.join(CATEGORIES)}"
        raise CodingTaskError(msg)
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
    truth.update({k: manifest[k] for k in ARTIFACT_MANIFEST_KEYS if k in manifest})
    tags = [*(manifest.get("tags") or []), "synthetic", "llm-authored"]
    row: dict[str, Any] = {
        "id": str(manifest.get("id") or path.name),
        "category": category,
        "difficulty": manifest.get("difficulty", "medium"),
        "prompt": f"{prompt_file.read_text(encoding='utf-8').strip()}\n\n{patch_request(category)}",
        "files": files,
        "truth": truth,
        "tags": list(dict.fromkeys(tags)),
    }
    if manifest.get("split"):
        row["split"] = manifest["split"]
    return row
