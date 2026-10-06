"""Patches: finding the one change an answer proposes, and applying it to a set of files.

An answer to a coding task is a single patch in one of two forms:

* a **unified diff** (``--- a/path``, ``+++ b/path``, ``@@`` hunks; ``git diff`` headers and
  ``/dev/null`` for created or deleted files are understood), or
* **file blobs**: the complete new content of each changed file, each introduced by a line
  ``=== path/to/file ===``.

Models get hunk line numbers and counts wrong all the time, so a diff is applied by matching the
lines a hunk expects (its context and removed lines) near where it says they are, not by trusting
its numbers. Paths are checked before anything is written: absolute paths, ``..``, ``.git`` and
other escapes are refused. ``extract_patch`` finds the patch in an answer: the ``patch`` field of
a JSON answer, else a fenced block, else the whole text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fusion.bench.sandbox import SandboxError, safe_relative_path
from fusion.orchestration.claims import patch_text_key

__all__ = [
    "MAX_PATCH_BYTES",
    "ExtractedPatch",
    "PatchError",
    "apply_patch",
    "diff_files",
    "extract_patch",
    "patch_as_blobs",
    "patch_text_key",
]

MAX_PATCH_BYTES = 1_000_000
_MAX_FILES = 100
_BLOB_HEADER = re.compile(r"^=== (?P<path>\S.*?) ===\s*$")
_HUNK_HEADER = re.compile(r"^@@ -(?P<old>\d+)(?:,(?P<oc>\d+))? \+(?P<new>\d+)(?:,(?P<nc>\d+))? @@")
_FENCE = re.compile(r"^(?P<ticks>`{3,})[^`]*$")


class PatchError(ValueError):
    """A patch that cannot be read or does not apply; the message says why."""


@dataclass(frozen=True)
class ExtractedPatch:
    """What ``extract_patch`` found: the patch, or why there is none."""

    text: str | None
    problem: str = ""  # set when ``text`` is None: "no patch" or "N different patches"


# -- finding the patch in an answer ---------------------------------------------------------------


def looks_like_patch(text: str) -> bool:
    lines = text.splitlines()
    if any(_BLOB_HEADER.match(line) for line in lines):
        return True
    has_hunk = any(_HUNK_HEADER.match(line) for line in lines)
    has_header = any(
        line.startswith("--- ") and nxt.startswith("+++ ")
        for line, nxt in zip(lines, lines[1:], strict=False)
    )
    return has_hunk and has_header or any(line.startswith("diff --git ") for line in lines)


def _fenced_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] | None = None
    ticks = ""
    for line in text.splitlines():
        stripped = line.strip()
        if current is None:
            match = _FENCE.match(stripped)
            if match:
                current, ticks = [], match.group("ticks")
        elif stripped.startswith(ticks) and not stripped.strip("`"):
            blocks.append("\n".join(current))
            current = None
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))  # an unclosed fence: a reply cut off by its token limit
    return blocks


def _from_json(text: str) -> tuple[bool, str | None]:
    """``(is a JSON object, its patch)``: the ``patch`` field of an object that has one."""
    start, end = text.find("{"), text.rfind("}") + 1
    if start < 0 or end <= start:
        return False, None
    try:
        data: Any = json.loads(text[start:end])
    except json.JSONDecodeError:
        return False, None
    if not isinstance(data, dict):
        return False, None
    patch = data.get("patch")
    return True, patch if isinstance(patch, str) and patch.strip() else None


def extract_patch(text: str, structured: Mapping[str, Any] | None = None) -> ExtractedPatch:
    """The single patch ``text`` proposes. ``structured`` is the run's parsed output, whose
    ``patch`` field wins. Several different patches in one answer is an error, not a guess."""
    field = (structured or {}).get("patch")
    if isinstance(field, str) and field.strip():
        return _checked(field)
    is_json, from_field = _from_json(text)
    if from_field is not None:
        return _checked(from_field)
    candidates = [b for b in _fenced_blocks(text) if looks_like_patch(b)]
    if not candidates and not is_json and looks_like_patch(text):
        candidates = [text]
    distinct = list(dict.fromkeys(patch_text_key(c) for c in candidates))
    if not distinct:
        return ExtractedPatch(None, "the answer contains no patch")
    if len(distinct) > 1:
        return ExtractedPatch(
            None, f"the answer contains {len(distinct)} different patches; it must hold one"
        )
    return _checked(candidates[0])


def _checked(text: str) -> ExtractedPatch:
    if len(text.encode("utf-8", errors="replace")) > MAX_PATCH_BYTES:
        return ExtractedPatch(None, f"the patch is larger than {MAX_PATCH_BYTES} bytes")
    if not looks_like_patch(text):
        return ExtractedPatch(None, "the patch field holds text that is not a diff or file blobs")
    return ExtractedPatch(text)


# -- applying -------------------------------------------------------------------------------------


@dataclass
class _Hunk:
    old_start: int
    old: list[str]
    new: list[str]


@dataclass
class _FilePatch:
    old_path: str | None  # None: the file is created
    new_path: str | None  # None: the file is deleted
    hunks: list[_Hunk]


def apply_patch(files: Mapping[str, str], patch: str) -> dict[str, str | None]:
    """The files ``patch`` changes: ``{path: new content}``, with ``None`` for a deleted file.

    ``files`` is the tree the patch applies to (path -> text). Raises ``PatchError`` for a patch
    that is malformed, names an unsafe path, or does not match the files.
    """
    if len(patch.encode("utf-8", errors="replace")) > MAX_PATCH_BYTES:
        msg = f"the patch is larger than {MAX_PATCH_BYTES} bytes"
        raise PatchError(msg)
    text = patch.replace("\r\n", "\n")
    if any(_BLOB_HEADER.match(line) for line in text.splitlines()):
        changes = _apply_blobs(text)
    else:
        changes = _apply_diff(files, text)
    if len(changes) > _MAX_FILES:
        msg = f"the patch changes {len(changes)} files; at most {_MAX_FILES} are allowed"
        raise PatchError(msg)
    return changes


def _clean_path(raw: str) -> str:
    try:
        return safe_relative_path(raw)
    except SandboxError as exc:
        raise PatchError(str(exc)) from exc


def _apply_blobs(text: str) -> dict[str, str | None]:
    changes: dict[str, str | None] = {}
    path: str | None = None
    body: list[str] = []

    def close() -> None:
        if path is None:
            return
        content = _unfence("\n".join(body))
        changes[path] = content if content.endswith("\n") or not content else content + "\n"

    for line in text.split("\n"):
        header = _BLOB_HEADER.match(line)
        if header:
            close()
            path, body = _clean_path(header.group("path")), []
        elif path is not None:
            body.append(line)
        elif line.strip():
            msg = "file blobs must each start with a line `=== path ===`"
            raise PatchError(msg)
    close()
    return changes


def _unfence(content: str) -> str:
    """A blob wrapped in a code fence (a model's habit) without the fence."""
    lines = content.strip("\n").split("\n")
    if len(lines) >= 2 and _FENCE.match(lines[0].strip()) and lines[-1].strip().strip("`") == "":
        return "\n".join(lines[1:-1])
    return content.strip("\n") if content.strip() else ""


def _strip_prefix(old: str | None, new: str | None, git: bool) -> tuple[str | None, str | None]:
    """Drop the ``a/`` and ``b/`` that ``git diff`` puts on paths (never a real directory's name
    unless the other side shows the same convention)."""
    a_side = old is None or old.startswith("a/")
    b_side = new is None or new.startswith("b/")
    if git or (a_side and b_side):
        old = old[2:] if old and old.startswith("a/") else old
        new = new[2:] if new and new.startswith("b/") else new
    return old, new


def _header_path(raw: str) -> str | None:
    path = raw.split("\t")[0].strip()
    if len(path) > 1 and path[0] == path[-1] == '"':
        path = path[1:-1]
    return None if path == "/dev/null" else path


def _parse_diff(text: str) -> list[_FilePatch]:
    lines = text.split("\n")
    patches: list[_FilePatch] = []
    i, git = 0, False
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            git = True
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old = _header_path(line[4:])
            new = _header_path(lines[i + 1][4:])
            old, new = _strip_prefix(old, new, git)
            hunks, i = _parse_hunks(lines, i + 2)
            if not hunks:
                msg = f"the diff for {new or old} has no hunks"
                raise PatchError(msg)
            patches.append(_FilePatch(old, new, hunks))
            continue
        i += 1
    if not patches:
        msg = "no file diffs found (expected `--- a/path`, `+++ b/path` and `@@` hunks)"
        raise PatchError(msg)
    return patches


def _parse_hunks(lines: list[str], i: int) -> tuple[list[_Hunk], int]:
    hunks: list[_Hunk] = []
    while i < len(lines):
        header = _HUNK_HEADER.match(lines[i])
        if not header:
            if hunks and (lines[i].startswith(("diff --git ", "--- ")) or lines[i].strip()):
                break
            i += 1
            continue
        entries: list[tuple[str, str]] = []
        i += 1
        while i < len(lines):
            line = lines[i]
            if _HUNK_HEADER.match(line) or line.startswith("diff --git "):
                break
            if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                break
            if line.startswith("\\"):  # "\ No newline at end of file"
                i += 1
                continue
            kind, content = (line[:1], line[1:]) if line else (" ", "")
            if kind not in "-+ ":
                break  # prose after the diff
            entries.append((kind, content))
            i += 1
        # Blank context a model left after the last change would only make the match stricter.
        while entries and entries[-1] == (" ", ""):
            entries.pop()
        hunks.append(
            _Hunk(
                int(header.group("old")),
                [c for k, c in entries if k in "- "],
                [c for k, c in entries if k in "+ "],
            )
        )
    return hunks, i


def _apply_diff(files: Mapping[str, str], text: str) -> dict[str, str | None]:
    changes: dict[str, str | None] = {}
    for file_patch in _parse_diff(text):
        old_path = _clean_path(file_patch.old_path) if file_patch.old_path else None
        new_path = _clean_path(file_patch.new_path) if file_patch.new_path else None
        target = new_path or old_path
        assert target is not None
        source = old_path or target
        current = changes.get(source, files.get(source)) if old_path else None
        if old_path and current is None:
            msg = f"{old_path}: the patch changes a file that does not exist"
            raise PatchError(msg)
        lines = _split_lines(current or "")
        ends_with_newline = current is None or current.endswith("\n") or not current
        result = _apply_hunks(target, lines, file_patch.hunks)
        if new_path is None:
            changes[old_path or target] = None
            continue
        content = "\n".join(result)
        changes[new_path] = content + "\n" if result and ends_with_newline else content
        if old_path and old_path != new_path:
            changes[old_path] = None  # a rename
    return changes


def _split_lines(text: str) -> list[str]:
    return text.split("\n")[:-1] if text.endswith("\n") else text.split("\n") if text else []


def _apply_hunks(path: str, lines: list[str], hunks: list[_Hunk]) -> list[str]:
    cursor = 0  # hunks are in order: each is searched for after the previous one
    out = list(lines)
    for number, hunk in enumerate(hunks, 1):
        at = _locate(out, hunk, cursor)
        if at is None:
            wanted = next((x for x in hunk.old if x.strip()), "")
            msg = f"{path}: hunk {number} does not apply (it expects a line like {wanted!r})"
            raise PatchError(msg)
        position, matched = at
        out[position : position + len(matched)] = hunk.new
        cursor = position + len(hunk.new)
    return out


def _locate(lines: list[str], hunk: _Hunk, cursor: int) -> tuple[int, list[str]] | None:
    """Where ``hunk``'s old lines are, nearest its stated line, as ``(index, the lines there)``."""
    old = hunk.old
    if not old:  # a pure addition: where it says, which for ``@@ -N,0`` is after line N
        return min(max(hunk.old_start, cursor), len(lines)), []
    wanted = max(hunk.old_start - 1, cursor)
    for normalise in (lambda s: s, str.rstrip, str.strip):
        key = [normalise(x) for x in old]
        found = [
            i
            for i in range(cursor, len(lines) - len(old) + 1)
            if [normalise(x) for x in lines[i : i + len(old)]] == key
        ]
        if found:
            best = min(found, key=lambda i: abs(i - wanted))
            return best, lines[best : best + len(old)]
    return None


# -- other forms ----------------------------------------------------------------------------------


def patch_as_blobs(files: Mapping[str, str], patch: str) -> str:
    """The same change as ``patch`` written as file blobs (deleted files cannot be blobs)."""
    changes = apply_patch(files, patch)
    parts = [
        f"=== {path} ===\n{content}" for path, content in changes.items() if content is not None
    ]
    return "\n".join(parts)


def diff_files(before: Mapping[str, str], after: Mapping[str, str]) -> str:
    """A unified diff taking the tree ``before`` to ``after`` (changed and created files)."""
    import difflib

    chunks: list[str] = []
    for path in sorted(after):
        new = after[path]
        old = before.get(path)
        if old == new:
            continue
        diff = list(
            difflib.unified_diff(
                (old or "").splitlines(),
                new.splitlines(),
                fromfile=f"a/{path}" if old is not None else "/dev/null",
                tofile=f"b/{path}",
                lineterm="",
                n=3,
            )
        )
        chunks.append("\n".join(diff))
    return "\n".join(chunks) + "\n" if chunks else ""
