"""What a simulated model writes for an executable coding task: a patch.

``BenchTask.simulated_truth`` turns a coding task's reference fix into a point ("solution") and
each plausible-but-wrong fix into a decoy ("flaw-N"), so the simulated models' usual machinery
decides, with skill, difficulty and correlated blind spots, whether a model finds the fix or falls
for a flaw. This module turns that decision into the patch a model would have written: the
reference solution, a flaw's patch, or (when it found nothing) the first flaw, the common wrong
answer. About a third of the time the patch is given as file blobs instead of a diff, so both
forms are exercised.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from fusion.bench.patch import PatchError, patch_as_blobs

if TYPE_CHECKING:
    from fusion.bench.spec import BenchTask, CodingTruth, TruthPoint
    from fusion.providers.simulated import SimWorld

__all__ = ["BLOB_SHARE", "coding_patch"]

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
