"""``fusion_propose_patch``: verified generation on a temporary copy of the user's project.

The panel writes candidate patches and the checks that judge them (``verified-synth``); this
module decides **where** those checks run and turns the result into what the host gets back.

* ``verify="sandbox"`` (the default): the project is copied, through the workspace reader (so
  ignored and secret files are never copied), into memory; every check and the optional
  ``tests_cmd`` run in throwaway sandbox copies (``fusion.verify.crosscheck``, no network, no
  writes outside the copy) and the best patch wins. The project directory is only read.
* ``verify="host"``: Fusion runs nothing. Checks are validated and identical patches grouped;
  the host gets the top two candidates and every valid check, runs them itself and calls
  ``fusion_select_patch`` with the results (``select_from_results``), which picks without a model.
* ``verify="none"``: no checks at all; the panel's patches are rivals and one is selected.

Whatever the mode, the patch returned is a **normalised unified diff** against the project as it
is (``normalize_patch``): a model's sloppy hunk numbers or whole-file blobs become a diff that
``git apply`` takes. It is returned as text; nothing here writes to the project.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from fusion.bench.patch import PatchError, apply_patch
from fusion.bench.sandbox import detect_isolation
from fusion.config.loader import VerifyConfig
from fusion.verify.crosscheck import (
    Cell,
    CheckEnv,
    CrossCheckReport,
    Matrix,
    check_records,
    crosscheck,
    pick,
)
from fusion.verify.synthesis import TASK_AUTHOR, Candidate, CheckScript
from fusion.workspace import ProjectCopy

__all__ = [
    "HOST_TOP",
    "PATCH_REQUEST",
    "TESTS_CHECK",
    "Crosschecker",
    "HostPick",
    "NormalizedPatch",
    "Verification",
    "VerifyMode",
    "choose_mode",
    "copy_fallback",
    "host_crosschecker",
    "host_report",
    "normalize_patch",
    "patch_prompt",
    "sandbox_crosschecker",
    "select_from_results",
    "tests_check",
]

VerifyMode = Literal["sandbox", "host", "none"]
Crosschecker = Callable[[list[Candidate]], Awaitable[CrossCheckReport]]
TESTS_CHECK = "tests_cmd"  # the name of the check that runs the caller's tests_cmd
HOST_TOP = 2  # host mode returns this many candidates
CELLS: frozenset[str] = frozenset({"pass", "fail", "error", "timeout"})

PATCH_REQUEST = (
    "Answer with ONE patch for the whole change, against the project files as shown, with "
    "paths relative to the project root: either a unified diff (`--- a/path`, `+++ b/path`, "
    "`@@` hunks) or the complete new content of every file you change, each introduced by a "
    "line `=== path/to/file ===`. Put the patch in the `patch` field of your JSON answer and "
    "nothing else in it (the claims describe what you changed and why). Change only what the "
    "task needs; do not weaken or delete tests."
)


def patch_prompt(task: str, tests_cmd: str | None) -> str:
    """The panel's task: the request, how to answer, and how the project's tests run."""
    parts = [task.strip(), "", PATCH_REQUEST]
    if tests_cmd:
        parts.append(
            f"The project's tests run with `{tests_cmd}` from the project root; a correct patch "
            "keeps them passing."
        )
    return "\n".join(parts)


# -- the patch the host gets ----------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedPatch:
    diff: str  # a unified diff ``git apply`` takes, against the project as it is
    files: list[str]  # the paths it changes, creates or deletes


class _Texts(Mapping[str, str]):
    """``files`` decoded on access; a binary file is absent. Lazy, so a project read on demand
    (``ProjectFiles``) is read only where a patch touches it."""

    def __init__(self, files: Mapping[str, bytes]) -> None:
        self._files = files

    def __getitem__(self, path: str) -> str:
        try:
            return self._files[path].decode("utf-8")
        except UnicodeDecodeError:
            raise KeyError(path) from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._files)

    def __len__(self) -> int:
        return len(self._files)


def _file_diff(path: str, old: str | None, new: str | None) -> str:
    before = (old or "").splitlines(keepends=True)
    after = (new or "").splitlines(keepends=True)
    lines = difflib.unified_diff(
        before,
        after,
        fromfile="/dev/null" if old is None else f"a/{path}",
        tofile="/dev/null" if new is None else f"b/{path}",
        n=3,
    )
    out: list[str] = []
    for line in lines:
        out.append(line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n")
    return "".join(out)


def normalize_patch(files: Mapping[str, bytes], patch: str) -> NormalizedPatch:
    """``patch`` (a diff or file blobs) as a clean unified diff against ``files``.

    ``PatchError`` when it is malformed, names an unsafe path, does not match the files, edits a
    binary file, or changes nothing.
    """
    texts = _Texts(files)
    changes = apply_patch(texts, patch)
    parts: list[str] = []
    touched: list[str] = []
    for path in sorted(changes):
        old, new = texts.get(path), changes[path]
        if path in files and path not in texts:
            msg = f"{path}: a binary file; a patch cannot change it"
            raise PatchError(msg)
        if old == new or (old is None and new is None):
            continue
        if old is None and new == "":
            parts.append(f"--- /dev/null\n+++ b/{path}\n")  # an empty new file: no hunk
        else:
            parts.append(_file_diff(path, old, new))
        touched.append(path)
    if not parts:
        msg = "the patch changes nothing"
        raise PatchError(msg)
    return NormalizedPatch("".join(parts), touched)


# -- which mode runs ------------------------------------------------------------------------------


def choose_mode(
    requested: VerifyMode, config: VerifyConfig, *, isolation: str | None = None
) -> tuple[VerifyMode, list[str]]:
    """The mode that will run and the warnings that explain any change from ``requested``.

    ``sandbox`` falls back to ``host`` when ``verify.sandbox_enabled`` is false or when this
    machine has no sandbox (``isolation``: ``detect_isolation()`` when None). A project too large
    to copy falls back later (``copy_fallback``).
    """
    if requested != "sandbox":
        return requested, []
    if not config.sandbox_enabled:
        return "host", [
            "verify.sandbox_enabled is false, so Fusion ran nothing: host mode, run the checks "
            "yourself"
        ]
    found = detect_isolation() if isolation is None else isolation
    if found == "none":
        return "host", [
            "No sandbox on this machine (it needs sandbox-exec on macOS or unshare on Linux), so "
            "Fusion ran nothing: host mode, run the checks yourself (`fusion doctor` says more)"
        ]
    return "sandbox", []


def copy_fallback(copy: ProjectCopy) -> str | None:
    """Why ``copy`` cannot be the sandbox's (the project is too large), or None when it can."""
    if copy.complete:
        return None
    return (
        f"Not copied for the sandbox: {copy.incomplete_reason}. Host mode: run the checks "
        "yourself (raise verify.max_copy_files or verify.max_copy_bytes to copy it)"
    )


# -- the cross-checkers the pipeline calls --------------------------------------------------------


@dataclass
class Verification:
    """What the cross-checker saw and found, for the tool's response (filled during the run)."""

    mode: VerifyMode
    cross_checks: bool = True  # the strategy cross-checks patches (verified-synth, or auto)
    candidates: list[Candidate] = field(default_factory=list)
    report: CrossCheckReport | None = None


def tests_check(command: str) -> CheckScript:
    """The caller's ``tests_cmd`` as a trusted check every candidate runs (bash, from the copy's
    root). Trusted: never validated or discarded, and it counts in full."""
    return CheckScript(name=TESTS_CHECK, language="shell", kind="invariant", body=command + "\n")


def sandbox_crosschecker(
    files: Mapping[str, bytes],
    *,
    tests_cmd: str | None,
    config: VerifyConfig,
    seen: Verification,
) -> Crosschecker:
    """Runs every candidate against every check (and ``tests_cmd``) in sandbox copies of
    ``files``. Isolation is required: without it a check is an ``error``, never a pass."""
    env = CheckEnv(
        source=dict(files),
        timeout_s=config.check_timeout_s,
        mem_mb=config.mem_mb,
        isolation="require",
        task_checks=(tests_check(tests_cmd),) if tests_cmd else (),
        task_timeout_s=config.tests_timeout_s,
        workers=config.workers,
    )

    async def check(candidates: list[Candidate]) -> CrossCheckReport:
        seen.candidates = list(candidates)
        seen.report = await asyncio.to_thread(crosscheck, candidates, env)
        return seen.report

    return check


def host_crosschecker(files: Mapping[str, bytes], *, seen: Verification) -> Crosschecker:
    """Validates and groups the candidates and runs nothing (``host_report``)."""

    async def check(candidates: list[Candidate]) -> CrossCheckReport:
        seen.candidates = list(candidates)
        # Normalising reads (and git-checks) the files each patch touches: off the event loop.
        seen.report = await asyncio.to_thread(host_report, candidates, files)
        return seen.report

    return check


def host_report(candidates: Sequence[Candidate], files: Mapping[str, bytes]) -> CrossCheckReport:
    """The cross-check without running anything: checks validated, patches normalised and
    grouped. One distinct patch wins outright; several are left to the selector, which ranks
    them for the host. The ``handoff`` keeps what ``fusion_select_patch`` needs later."""
    started = time.monotonic()
    records = check_records(candidates)
    groups: dict[str, str] = {}
    errors: dict[str, str] = {}
    diffs: dict[str, str] = {}
    first_of: dict[str, str] = {}
    for candidate in candidates:
        try:
            diff = normalize_patch(files, candidate.patch).diff
        except PatchError as exc:
            errors[candidate.id] = str(exc)[:300]
            groups[candidate.id] = candidate.id
            continue
        key = hashlib.sha256(diff.encode()).hexdigest()
        groups[candidate.id] = first_of.setdefault(key, candidate.id)
        diffs[candidate.id] = diff
    reps = [c.id for c in candidates if groups[c.id] == c.id and c.id not in errors]
    if len(reps) == 1:
        winner, tied = reps[0], []
        reason = f"{reps[0]}'s patch is the only distinct one that applies; nothing was run"
    elif reps:
        winner, tied = None, reps
        reason = f"host mode: nothing was run; {len(reps)} distinct patches"
    else:
        winner, tied = None, []
        reason = "no candidate patch applies"
    kept = [r for r in records if r.status == "kept"]
    handoff = {
        "candidates": [
            {"id": c.id, "group": groups[c.id], "patch": diffs.get(c.id), "error": errors.get(c.id)}
            for c in candidates
        ],
        "checks": [
            {
                "id": r.id,
                "author": r.author,
                "name": r.check.name,
                "language": r.check.language,
                "kind": r.check.kind,
                "body": r.check.body,
            }
            for r in kept
        ],
    }
    return CrossCheckReport(
        checks=records,
        matrix=Matrix(
            cells={c.id: {} for c in candidates},
            authors={r.id: r.author for r in records},
            groups=groups,
            failed=frozenset(errors),
        ),
        groups=groups,
        patch_errors=errors,
        scores={c.id: 0.0 for c in candidates},
        shares={},  # nothing ran: no share passed, and no reason for the ladder to climb
        winner=winner,
        tied=tied,
        reason=reason,
        guardrails={c.id: list(c.guardrails) for c in candidates if c.guardrails},
        isolation="none",
        seconds=time.monotonic() - started,
        handoff=handoff,
    )


# -- host mode, second half: the host's results -> a pick ---------------------------------------


@dataclass
class HostPick:
    winner: str | None
    patch: str | None
    reason: str
    scores: dict[str, float]
    tied: list[str]
    dropped: dict[str, str]  # check id -> why its results were not counted
    host_checks: list[str]  # ids the host added (its own tests); they count in full


def select_from_results(
    record: Mapping[str, Any],
    results: Mapping[str, Mapping[str, str]],
    baseline: Mapping[str, str] | None = None,
) -> HostPick:
    """Pick a patch from the host's check results, with the same scoring as the sandbox.

    ``record`` is the run's ``disagreement.crosscheck`` (host mode); ``results`` maps a candidate
    to ``{check id: pass|fail|error|timeout}``; ``baseline`` the checks' results on the code as it
    is, which drops the checks that cannot discriminate as the sandbox does. A check id the run
    did not hand out is the host's own (its tests) and counts in full. No model is called.
    """
    handoff = record.get("handoff")
    if not isinstance(handoff, Mapping):
        msg = "that run was not a host-mode fusion_propose_patch run (it has no candidates)"
        raise ValueError(msg)
    candidates = {str(c["id"]): c for c in handoff.get("candidates", [])}
    checks = {str(k["id"]): k for k in handoff.get("checks", [])}
    unknown = sorted(set(results) - set(candidates))
    if unknown:
        msg = f"unknown candidate(s) {', '.join(unknown)}; the run has {', '.join(candidates)}"
        raise ValueError(msg)
    bad = sorted(
        {v for row in results.values() for v in row.values()} | set((baseline or {}).values())
    )
    bad = [v for v in bad if v not in CELLS]
    if bad:
        msg = f"results must be pass, fail, error or timeout, not {', '.join(map(repr, bad))}"
        raise ValueError(msg)
    dropped: dict[str, str] = {}
    for check_id, cell in (baseline or {}).items():
        found = checks.get(check_id)
        if found is None:
            continue
        if found["kind"] == "invariant" and cell != "pass":
            dropped[check_id] = f"an invariant must pass on the code as it is; it gave {cell}"
        elif found["kind"] != "invariant" and cell == "pass":
            dropped[check_id] = "passes on the code as it is, so it cannot tell a fix from none"
    groups = {str(k): str(v) for k, v in (record.get("groups") or {}).items()}
    cells: dict[str, dict[str, Cell]] = {
        cid: {k: v for k, v in row.items() if k not in dropped}  # type: ignore[misc]
        for cid, row in results.items()
    }
    host_checks = sorted({k for row in results.values() for k in row} - set(checks))
    authors = {k: str(v["author"]) for k, v in checks.items()}
    authors.update({k: TASK_AUTHOR for k in host_checks})
    failed = frozenset(c for c in cells if not candidates[c].get("patch"))
    chosen = pick(
        Matrix(
            cells=cells,
            authors=authors,
            groups={c: groups.get(c, c) for c in cells},
            failed=failed,
        )
    )
    winner = chosen.winner or (chosen.tied[0] if chosen.tied else None)
    reason = chosen.reason
    if chosen.winner is None and winner is not None:
        reason += f"; took {winner}, the first of them (they pass the same checks)"
    patch = candidates[winner].get("patch") if winner else None
    return HostPick(
        winner=winner,
        patch=str(patch) if patch else None,
        reason=reason,
        scores=chosen.scores,
        tied=chosen.tied,
        dropped=dropped,
        host_checks=host_checks,
    )
