"""What a simulated model writes for an executable coding task: a patch.

``BenchTask.simulated_truth`` turns a coding task's reference fix into a point ("solution") and
each plausible-but-wrong fix into a decoy ("flaw-N"), so the simulated models' usual machinery
decides, with skill, difficulty and correlated blind spots, whether a model finds the fix or falls
for a flaw. This module turns that decision into the patch a model would have written: the
reference solution, a flaw's patch, or (when it found nothing) the first flaw, the common wrong
answer. About a third of the time the patch is given as file blobs instead of a diff, so both
forms are exercised.

Asked for checks (``verified-synth``), a model writes a check of what it believes: that the files
end up as its own patch leaves them. A model that understood the task (a draw on its skill,
separate from the patch, so a model can botch the fix and still know what a fix must do) also
writes a check that the files end up as the reference fix leaves them. Both compare file hashes,
so they are oracle-like: a simulated cross-check is an upper bound on what real checks do, which
Task 16 measures with real models. The hash reveals nothing a prompt could use.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from fusion.bench.patch import PatchError, apply_patch, patch_as_blobs

if TYPE_CHECKING:
    from fusion.bench.spec import BenchTask, CodingTruth, TruthPoint
    from fusion.providers.simulated import SimWorld

__all__ = ["BLOB_SHARE", "coding_checks", "coding_patch"]

BLOB_SHARE = 0.35  # share of patches written as file blobs rather than a unified diff


def coding_patch(
    task: BenchTask,
    truth: CodingTruth,
    said: Iterable[TruthPoint],
    *,
    model: str,
    world: SimWorld,
    seed: int | None,
) -> str:
    """The patch a model that said the points ``said`` (the fix and/or flaws) would write."""
    ids = [p.id for p in said]
    if "solution" in ids:
        patch = truth.solution
    else:
        flaw = next((p.id for p in said if p.id.startswith("flaw-")), None)
        index = int(flaw.split("-")[1]) - 1 if flaw else 0
        patch = truth.flaws[index].patch if index < len(truth.flaws) else ""
    if patch and world.uniform("patch-form", model, task.id, seed) < BLOB_SHARE:
        try:
            return patch_as_blobs(task.files, patch)
        except PatchError:
            return patch  # a reference patch that does not apply is the validator's finding
    return patch


_HASH_CHECK = """import hashlib
import pathlib
import sys

want = {want!r}
ok = all(
    (h is None and not pathlib.Path(p).exists())
    or (h is not None and pathlib.Path(p).is_file()
        and hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() == h)
    for p, h in want.items()
)
sys.exit(0 if ok else 1)
"""


def _hash_check(name: str, task: BenchTask, patch: str) -> dict[str, Any] | None:
    try:
        changes = apply_patch(task.files, patch)
    except PatchError:
        return None
    want = {
        path: None if content is None else hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in sorted(changes.items())
    }
    if not want:
        return None
    body = _HASH_CHECK.format(want=want)
    return {"name": name, "language": "python", "kind": "repro", "body": body}


def coding_checks(
    task: BenchTask, truth: CodingTruth, patch: str, *, understood: bool
) -> list[dict[str, Any]]:
    """The checks a model whose patch is ``patch`` writes: one of its own belief, and, when it
    ``understood`` the task, one of the reference fix (the same check if its patch is the fix)."""
    checks = [c for c in [_hash_check("own_belief", task, patch)] if c]
    if understood and truth.solution:
        intent = _hash_check("task_intent", task, truth.solution)
        if intent and all(intent["body"] != c["body"] for c in checks):
            checks.append(intent)
    return checks
