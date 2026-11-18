"""Compose: build final packs from the edits that were measured, using their behavioural contrasts.

Composer 1 is deterministic (``merge_edits``: every edit that helped, appends in order of gain, the
best ``replace`` of a prompt winning over the others). Composers 2..K are models shown each edit
with its measured effect (``contrast``) and asked for one final set of edits; they may narrow a
``confounded`` edit to the task types it helped. Every final is validated like a proposal before
it is probed.
"""

from __future__ import annotations

from collections.abc import Sequence

from fusion.tune.probe import Effect
from fusion.tune.propose import Edit

__all__ = ["contrast", "merge_edits"]


def merge_edits(measured: Sequence[tuple[Edit, float]]) -> list[Edit]:
    """One set of edits from ``(edit, gain)`` pairs that each helped alone.

    Appends to a prompt are all kept, strongest first. A ``replace`` rewrites the whole prompt, so
    only the strongest replace of a prompt is kept, and it goes first (the appends then add to it).
    """
    ranked = sorted(measured, key=lambda pair: -pair[1])
    replaces: dict[str, Edit] = {}
    appends: list[Edit] = []
    for edit, _ in ranked:
        if edit.op == "replace":
            replaces.setdefault(edit.target, edit)
        else:
            appends.append(edit)
    return [*replaces.values(), *appends]


def contrast(row_id: str, edit: Edit, effect: Effect) -> str:
    """One measured edit as a composer reads it: what changed, what it did, and where."""
    cats = ", ".join(f"{c} {d:+.3f}" for c, d in effect.by_category.items()) or "none"
    return "\n".join(
        [
            f"\n### Edit {row_id}: {edit.op} to prompt `{edit.target}`",
            f"Text: {edit.text.strip()}",
            f"Effect on mean quality: {effect.text()} over {effect.n} task(s), noise band "
            f"{effect.band:.3f}; better on {effect.improved}, worse on {effect.worsened}",
            f"By task type: {cats}",
            f"Verdict: {effect.verdict} ({effect.reason})",
        ]
    )
