"""Cross-verification: every candidate patch against every check, in sandbox copies, then a pick.

``crosscheck(candidates, env)`` does four things, all on throwaway copies of ``env.source``:

1. **Validate** each candidate's checks statically (``validate.validate_check``); an invalid check
   is dropped with its reasons and never runs.
2. **Baseline**: run every valid check on the *unpatched* copy. A ``repro`` or ``property`` check
   that already passes cannot tell a fix from no fix, so it is discarded; an ``invariant`` that
   already fails states something the code does not do, so it is discarded too ("broken").
3. **Matrix**: candidates whose patches produce the same files form one group (they behave the
   same, so they are run once); each group's patch is run against every kept check, plus the
   task's own checks (its visible tests) when it has any. A cell is ``pass``, ``fail``, ``error``
   (the patch did not apply, the check could not start or was killed) or ``timeout``.
4. **Pick** (``pick``): a candidate scores one point per check it passes that someone else wrote
   (another candidate, or the task) and half a point per check its own group wrote. Checks written
   independently of a patch are the stronger evidence, so two members agreeing on a wrong patch
   cannot outvote one that passes everyone's checks: CodeT's dual agreement, with the author
   kept apart. The best score wins; a tie between groups is left for the ``select`` aggregator.

Each check runs in a **fresh copy** (``SandboxCopy``) under ``fusion.bench.sandbox`` limits: no
network and no writes outside the copy where the platform allows, a wall-clock limit, CPU, memory
and output caps, a scrubbed environment. A check that loops is killed and scored ``timeout``; one
that opens a socket or writes outside fails. Nothing here ever writes to ``env.source``: it is read
once, into memory, and every copy is deleted when its run ends (Part 3, rule 5).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from fusion.bench.patch import PatchError, apply_patch
from fusion.bench.sandbox import Isolation, Sandbox, SandboxError, SandboxLimits
from fusion.verify.synthesis import TASK_AUTHOR, Candidate, CheckScript
from fusion.verify.validate import validate_check

__all__ = [
    "OWN_WEIGHT",
    "Cell",
    "CheckEnv",
    "CheckRecord",
    "CrossCheckReport",
    "Matrix",
    "Pick",
    "SandboxCopy",
    "check_records",
    "crosscheck",
    "patched_files",
    "pick",
    "run_check",
    "snapshot",
]

Cell = Literal["pass", "fail", "error", "timeout"]
CheckStatus = Literal["kept", "invalid", "not_discriminating", "broken"]
OWN_WEIGHT = 0.5  # a check from the candidate's own group; checks by others count 1.0
_EPS = 1e-9
_SKIP_PARTS = frozenset({".git", ".hg", ".svn"})


# -- the copy -------------------------------------------------------------------------------------


Source = Path | Mapping[str, str | bytes]


def snapshot(source: Source) -> dict[str, bytes]:
    """The files of ``source`` (a directory, or path -> content) in memory, read once.

    From a directory: regular files only, no symlinks and nothing under ``.git``. The directory is
    only read.
    """
    if not isinstance(source, Path):
        return {p: c.encode("utf-8") if isinstance(c, str) else c for p, c in source.items()}
    files: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if _SKIP_PARTS & set(rel.parts) or path.is_symlink() or not path.is_file():
            continue
        files[rel.as_posix()] = path.read_bytes()
    return files


def _texts(files: Mapping[str, bytes]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for path, data in files.items():
        try:
            texts[path] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue  # a binary file: a patch can replace it whole, not edit it
    return texts


def patched_files(files: Mapping[str, bytes], patch: str | None) -> dict[str, bytes]:
    """``files`` with ``patch`` applied (``PatchError`` when it does not apply)."""
    out = dict(files)
    if patch:
        for path, content in apply_patch(_texts(files), patch).items():
            if content is None:
                out.pop(path, None)
            else:
                out[path] = content.encode("utf-8")
    return out


class SandboxCopy:
    """A temporary copy of ``source`` (with ``patch`` applied) in a ``Sandbox``, deleted on exit.

    The only way verify code touches a project: ``with SandboxCopy(src, patch=p) as box:``.
    ``PatchError`` when the patch does not apply.
    """

    def __init__(
        self,
        source: Source,
        *,
        patch: str | None = None,
        limits: SandboxLimits | None = None,
        isolation: Isolation | None = None,
    ) -> None:
        self.source = source
        self.patch = patch
        self.limits = limits or SandboxLimits()
        self.isolation = isolation
        self._box: Sandbox | None = None

    def __enter__(self) -> Sandbox:
        files = patched_files(snapshot(self.source), self.patch)
        box = Sandbox(isolation=self.isolation, limits=self.limits)
        box.__enter__()
        try:
            box.copy_in(files)
        except BaseException:
            box.__exit__(None, None, None)
            raise
        self._box = box
        return box

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._box is not None:
            self._box.__exit__(exc_type, exc, tb)
            self._box = None


def run_check(box: Sandbox, check: CheckScript, *, timeout_s: float) -> Cell:
    """Run one check in ``box``'s work directory: exit 0 is ``pass``, any other exit ``fail``,
    a kill by the clock ``timeout``, one that could not start or died by a signal ``error``."""
    suffix = "py" if check.language == "python" else "sh"
    script = box.write_aux(f"checks/{_safe(check.name)}.{suffix}", check.body)
    command = ["python" if check.language == "python" else "bash", str(script)]
    result = box.run(command, timeout=timeout_s, env={"PYTHONPATH": str(box.work)})
    if result.timed_out:
        return "timeout"
    if result.returncode is None or result.returncode < 0:
        return "error"
    return "pass" if result.returncode == 0 else "fail"


def _safe(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in name)[:48]
    return cleaned or "check"


# -- the matrix and the pick ----------------------------------------------------------------------


@dataclass
class Matrix:
    """``cells[candidate][check]``; ``authors[check]`` is the candidate that wrote it (or
    ``"task"``); ``groups[candidate]`` the group whose patch produces the same files; ``failed``
    the candidates whose patch did not apply (they cannot win)."""

    cells: dict[str, dict[str, Cell]]
    authors: dict[str, str]
    groups: dict[str, str]
    failed: frozenset[str] = frozenset()


@dataclass
class Pick:
    winner: str | None  # None: a tie between groups (``tied``), or no patch applied
    tied: list[str]  # one candidate per tied group, in panel order
    scores: dict[str, float]
    reason: str


def _weight(matrix: Matrix, candidate: str, check: str) -> float:
    group = matrix.groups.get(candidate, candidate)
    author = matrix.authors.get(check, TASK_AUTHOR)
    own = author != TASK_AUTHOR and matrix.groups.get(author, author) == group
    return OWN_WEIGHT if own else 1.0


def pick(matrix: Matrix) -> Pick:
    """The candidate with the best weighted score (see the module docstring)."""
    scores = {
        c: sum(_weight(matrix, c, k) for k, cell in row.items() if cell == "pass")
        for c, row in matrix.cells.items()
    }
    eligible = [c for c in matrix.cells if c not in matrix.failed]
    if not eligible:
        return Pick(None, [], scores, "no candidate patch applied")
    top = max(scores[c] for c in eligible)
    best: list[str] = []
    for c in eligible:  # one per group, the first member in panel order
        group = matrix.groups.get(c, c)
        if abs(scores[c] - top) < _EPS and group not in {matrix.groups.get(b, b) for b in best}:
            best.append(c)
    if len(best) > 1:
        return Pick(None, best, scores, f"tie at {top:.1f} between {', '.join(best)}")
    winner = best[0]
    row = matrix.cells[winner]
    passed = [k for k, cell in row.items() if cell == "pass"]
    foreign = sum(1 for k in passed if _weight(matrix, winner, k) == 1.0)
    others = [
        (scores[c], c) for c in eligible if matrix.groups.get(c, c) != matrix.groups.get(winner)
    ]
    runner = max(others) if others else None
    tail = f"; next best {runner[1]} {runner[0]:.1f}" if runner else ""
    reason = (
        f"{winner} passed {len(passed)} of {len(row)} checks ({foreign} written by other "
        f"candidates or the task), score {top:.1f}{tail}"
    )
    return Pick(winner, [], scores, reason)


# -- the whole cross-check ------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckEnv:
    """Where and how checks run: a copy of ``source`` per run, under these limits.
    ``task_checks`` come with the task (its visible tests): trusted, never validated or
    discarded, and they count in full for every candidate."""

    source: Source
    timeout_s: float = 10.0
    mem_mb: int | None = 512
    isolation: Isolation | None = None
    task_checks: tuple[CheckScript, ...] = ()
    task_timeout_s: float | None = None  # for task_checks; None: timeout_s
    workers: int = 4  # sandbox runs at once


@dataclass
class CheckRecord:
    id: str
    author: str
    check: CheckScript
    status: CheckStatus = "kept"
    reasons: list[str] = field(default_factory=list)
    baseline: Cell | None = None  # the result on the unpatched copy; None: never ran

    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "author": self.author,
            "kind": self.check.kind,
            "language": self.check.language,
            "status": self.status,
            "reasons": self.reasons,
            "baseline": self.baseline,
        }


@dataclass
class CrossCheckReport:
    """Everything the cross-check found; ``record()`` is what the run record keeps."""

    checks: list[CheckRecord]
    matrix: Matrix
    groups: dict[str, str]
    patch_errors: dict[str, str]
    scores: dict[str, float]
    shares: dict[str, float | None]  # share of the kept checks passed; -1.0: the patch failed
    winner: str | None
    tied: list[str]
    reason: str
    guardrails: dict[str, list[str]] = field(default_factory=dict)
    isolation: str = "none"
    seconds: float = 0.0
    # Host mode (``fusion.verify.propose``): what the host needs later to pick, kept with the run
    # (each candidate's normalised patch and every valid check, with its body).
    handoff: dict[str, Any] | None = None

    @property
    def kept(self) -> list[CheckRecord]:
        return [c for c in self.checks if c.status == "kept"]

    def record(self) -> dict[str, Any]:
        return {
            "winner": self.winner,
            "tied": self.tied,
            "reason": self.reason,
            "scores": {c: round(s, 3) for c, s in self.scores.items()},
            "shares": {c: None if s is None else round(s, 3) for c, s in self.shares.items()},
            "groups": self.groups,
            "patch_errors": self.patch_errors,
            "checks": [c.record() for c in self.checks],
            "matrix": self.matrix.cells,
            "guardrails": self.guardrails,
            "isolation": self.isolation,
            "seconds": round(self.seconds, 2),
            **({"handoff": self.handoff} if self.handoff is not None else {}),
        }


def check_records(
    candidates: Sequence[Candidate], task_checks: Sequence[CheckScript] = ()
) -> list[CheckRecord]:
    """One record per check with a unique id (``author/name``), each candidate check validated
    (``invalid`` with its reasons when it fails validation); the task's checks are trusted."""
    return _records(candidates, CheckEnv(source={}, task_checks=tuple(task_checks)))


def _records(candidates: Sequence[Candidate], env: CheckEnv) -> list[CheckRecord]:
    records: list[CheckRecord] = []
    used: set[str] = set()

    def ident(author: str, name: str) -> str:
        base = f"{author}/{_safe(name)}"
        unique, n = base, 1
        while unique in used:
            n += 1
            unique = f"{base}-{n}"
        used.add(unique)
        return unique

    for check in env.task_checks:
        records.append(CheckRecord(ident(TASK_AUTHOR, check.name), TASK_AUTHOR, check))
    for candidate in candidates:
        for check in candidate.checks:
            rec = CheckRecord(ident(candidate.id, check.name), candidate.id, check)
            rec.reasons = validate_check(check)
            if rec.reasons:
                rec.status = "invalid"
            records.append(rec)
    return records


def _run_one(
    source: Mapping[str, bytes], patch: str | None, rec: CheckRecord, env: CheckEnv
) -> tuple[Cell, str]:
    """``(cell, isolation)`` for one check in a fresh copy of ``source`` with ``patch``."""
    timeout = env.timeout_s
    if rec.author == TASK_AUTHOR and env.task_timeout_s is not None:
        timeout = env.task_timeout_s
    limits = SandboxLimits(timeout_s=timeout, mem_mb=env.mem_mb, max_output_bytes=20_000)
    try:
        with SandboxCopy(source, patch=patch, limits=limits, isolation=env.isolation) as box:
            return run_check(box, rec.check, timeout_s=timeout), box.isolation
    except (PatchError, SandboxError, OSError):
        return "error", "none"


def _group_key(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(json.dumps(path).encode())
        digest.update(hashlib.sha256(files[path]).digest())
    return digest.hexdigest()


def crosscheck(candidates: Sequence[Candidate], env: CheckEnv) -> CrossCheckReport:
    """Validate, baseline and cross-run the candidates' checks; pick a patch. Blocking."""
    started = time.monotonic()
    files = snapshot(env.source)
    records = _records(candidates, env)
    isolation = {"none"}
    pool = ThreadPoolExecutor(max_workers=max(1, env.workers))
    try:
        # 2. the baseline: every valid candidate check on the unpatched copy
        to_baseline = [r for r in records if r.status == "kept" and r.author != TASK_AUTHOR]
        for rec, (cell, iso) in zip(
            to_baseline,
            pool.map(lambda r: _run_one(files, None, r, env), to_baseline),
            strict=True,
        ):
            rec.baseline = cell
            isolation.add(iso)
            if rec.check.kind == "invariant" and cell != "pass":
                rec.status = "broken"
                rec.reasons.append(f"an invariant must pass on the code as it is; it gave {cell}")
            elif rec.check.kind != "invariant" and cell == "pass":
                rec.status = "not_discriminating"
                rec.reasons.append("passes on the code as it is, so it cannot tell a fix from none")
        kept = [r for r in records if r.status == "kept"]

        # 3. groups of identical results, then the matrix
        groups: dict[str, str] = {}
        patch_errors: dict[str, str] = {}
        first_of: dict[str, str] = {}
        for candidate in candidates:
            try:
                key = _group_key(patched_files(files, candidate.patch))
            except PatchError as exc:
                patch_errors[candidate.id] = str(exc)[:300]
                groups[candidate.id] = candidate.id
                continue
            groups[candidate.id] = first_of.setdefault(key, candidate.id)
        reps = [c for c in candidates if groups[c.id] == c.id and c.id not in patch_errors]
        jobs = [(rep, rec) for rep in reps for rec in kept]
        results = list(pool.map(lambda j: _run_one(files, j[0].patch, j[1], env), jobs))
    finally:
        pool.shutdown(wait=True)
    rows: dict[str, dict[str, Cell]] = {rep.id: {} for rep in reps}
    for (rep, rec), (cell, iso) in zip(jobs, results, strict=True):
        rows[rep.id][rec.id] = cell
        isolation.add(iso)
    cells: dict[str, dict[str, Cell]] = {}
    for candidate in candidates:
        if candidate.id in patch_errors:
            cells[candidate.id] = {rec.id: "error" for rec in kept}
        else:
            cells[candidate.id] = dict(rows[groups[candidate.id]])
    matrix = Matrix(
        cells=cells,
        authors={r.id: r.author for r in records},
        groups=groups,
        failed=frozenset(patch_errors),
    )
    chosen = pick(matrix)
    shares: dict[str, float | None] = {}
    for candidate in candidates:
        row = cells[candidate.id]
        if candidate.id in patch_errors:
            shares[candidate.id] = -1.0
        else:
            shares[candidate.id] = (
                sum(v == "pass" for v in row.values()) / len(row) if row else None
            )
    enforced = sorted(isolation - {"none"})
    return CrossCheckReport(
        checks=records,
        matrix=matrix,
        groups=groups,
        patch_errors=patch_errors,
        scores=chosen.scores,
        shares=shares,
        winner=chosen.winner,
        tied=chosen.tied,
        reason=chosen.reason,
        guardrails={c.id: list(c.guardrails) for c in candidates if c.guardrails},
        isolation=enforced[0] if enforced else "none",
        seconds=time.monotonic() - started,
    )
