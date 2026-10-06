"""What every evaluator shares: the ``Evidence`` it returns and the way it reads an answer's files.

An evaluator looks at the *artefacts* of an answer (the tree its patch produces) the way a human
reviewer would: run the tests, lint the code, time it, open the page. It never judges; it measures
and reports ``Evidence``, which a scorer turns into gates and criteria and an agentic judge may
cite by id. Evaluators run code the model wrote, so they run it only inside ``Sandbox`` (or, for
the browser, behind the guards ``visual`` describes), on a *copy* of the answer's tree: the
``workdir`` they are given is only ever read.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from fusion.bench.sandbox import RunResult, Sandbox, SandboxLimits
from fusion.bench.spec import BenchTask, EvidenceKind

__all__ = [
    "EVIDENCE_KINDS",
    "Evaluator",
    "Evidence",
    "Memo",
    "ToolSpec",
    "SLOTS",
    "read_tree",
    "run_in_sandbox",
    "tree_digest",
]

EVIDENCE_KINDS: tuple[EvidenceKind, ...] = (
    "tests",
    "build",
    "static",
    "perf",
    "screenshot",
    "a11y",
    "console",
    "diff_stats",
)
_SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".git"}
_MAX_FILE_BYTES = 400_000
_MAX_FILES = 400
SLOTS = threading.BoundedSemaphore(max(2, min(os.cpu_count() or 4, 6)))  # sandboxes at once


class Evidence(BaseModel):
    """One piece of evidence about one answer: everything a judge may cite.

    ``ok`` is the evaluator's verdict (None: it could not decide, see ``status``). ``metrics`` are
    numbers a gate or a criterion can read. ``artifact_path`` is a file the evaluator produced
    (a screenshot). ``cost_usd`` is what taking it cost in model money (none of the built-in
    evaluators call a model) and ``seconds`` how long it took: both are *eval* cost and time, kept
    apart from the arm's own.
    """

    kind: EvidenceKind
    ok: bool | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    artifact_path: Path | None = None
    summary: str = ""
    cost_usd: float = 0.0
    # Beyond the roadmap's fields:
    id: str = ""  # "E1", "E2"...: assigned when a scorer collects the evidence; judges cite it
    name: str = ""  # the evaluator that took it
    seconds: float = 0.0
    status: Literal["measured", "skipped", "unstable"] = "measured"
    artifacts: dict[str, Path] = Field(default_factory=dict)  # further files, by name

    def line(self) -> str:
        verdict = {True: "PASS", False: "FAIL", None: "n/a"}[self.ok]
        shown = ", ".join(f"{k}={v:g}" for k, v in sorted(self.metrics.items())[:8])
        text = f"[{self.id or self.kind}] {self.kind} {verdict}"
        if self.status != "measured":
            text += f" ({self.status})"
        if shown:
            text += f" {shown}"
        return f"{text}: {self.summary}" if self.summary else text


class ToolSpec(BaseModel):
    """A tool the agentic judge may call, described for the model (function-calling style)."""

    name: str
    description: str
    input_schema: dict[str, object]


class Evaluator(Protocol):
    """Measures one aspect of an answer's tree. ``workdir`` is read, never written."""

    name: str

    async def run(self, workdir: Path, task: BenchTask) -> Evidence: ...


def read_tree(root: Path) -> dict[str, str]:
    """The text files under ``root`` as ``{relative posix path: content}`` (symlinks, binary and
    oversized files are left out, as is anything in a cache or VCS directory)."""
    tree: dict[str, str] = {}
    for item in sorted(root.rglob("*")):
        rel = item.relative_to(root)
        if (
            not item.is_file()
            or item.is_symlink()
            or any(part in _SKIP_DIRS for part in rel.parts)
            or item.stat().st_size > _MAX_FILE_BYTES
        ):
            continue
        try:
            tree[rel.as_posix()] = item.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if len(tree) >= _MAX_FILES:
            break
    return tree


def tree_digest(tree: dict[str, str]) -> str:
    """Fingerprint of a tree's paths and contents."""
    digest = hashlib.sha256()
    for path in sorted(tree):
        digest.update(path.encode() + b"\0" + tree[path].encode() + b"\0")
    return digest.hexdigest()[:20]


class Memo:
    """A small thread-safe cache for evidence that has no files behind it: the same tree of the
    same task gives the same evidence, so arms and repeats that gave the same answer share it."""

    def __init__(self, limit: int = 2048) -> None:
        self._items: dict[str, Evidence] = {}
        self._lock = threading.Lock()
        self._limit = limit

    def get_or_run(self, key: str, make: Callable[[], Evidence]) -> Evidence:
        with self._lock:
            hit = self._items.get(key)
        if hit is not None:
            return hit.model_copy(deep=True)
        evidence = make()
        if evidence.status != "unstable":  # a noisy measurement is worth taking again
            with self._lock:
                if len(self._items) >= self._limit:
                    self._items.pop(next(iter(self._items)))
                self._items[key] = evidence
        return evidence.model_copy(deep=True)


def run_in_sandbox(
    tree: Mapping[str, str],
    cmd: Sequence[str],
    *,
    timeout_s: float = 30.0,
    mem_mb: int | None = 1024,
    extra: Mapping[str, str | bytes] | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[RunResult, str]:
    """Run ``cmd`` over a copy of ``tree`` (plus ``extra`` files laid over it) in a fresh
    sandbox. Returns the result and the isolation the sandbox enforced. Blocking."""
    limits = SandboxLimits(timeout_s=timeout_s, mem_mb=mem_mb)
    with SLOTS, Sandbox(limits=limits) as box:
        box.copy_in(tree)
        if extra:
            box.copy_in(extra)
        return box.run(cmd, env=env), box.isolation
