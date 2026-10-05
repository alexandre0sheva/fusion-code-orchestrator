"""Building datasets: compile hand-reviewable authoring files into the JSONL a study loads, and
(optionally, on a spend cap) have a model draft candidate tasks for a person to review.

**Authoring format.** One YAML file per category in a directory (``evals/datasets/authoring/v1``),
each a list of items. The compiler turns them into ``BenchTask`` JSONL with the right line numbers,
so nobody counts lines by hand and the JSONL never drifts from the sources (a test compares them).

``code_review`` item: ``id, difficulty, split, language, title, description, files, bugs``.
Each file has a ``path`` and a ``diff`` whose every line starts with ``=`` (unchanged), ``+``
(added) or ``-`` (removed). The post-change file is the ``=`` and ``+`` lines. A bug is located
by writing ``«id»`` at the end of the added line it is on; the marker is removed from the code.
No bugs (or no ``bugs`` key) is a clean change. The task shows the model the diff with the new
file's line numbers in a gutter; the truth is ``bugs`` as ``ReviewTruth`` reads them.

``debugging`` item: ``id, difficulty, split, language, symptom, files [{path, content}], trace
(a stack trace or any failure output), logs, root_cause_tags, root_cause_aliases, root_cause,
fix_keywords``.

``architecture`` / ``planning`` item: ``id, difficulty, split, tags, prompt, context, required,
forbidden``; each point is ``{text, keywords, gate?, weight?}``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from fusion.bench.spec import BenchTask, Category
from fusion.config.layers import ConfigError

__all__ = [
    "AUTHORING_DIR",
    "CATEGORY_FILES",
    "AuthoringError",
    "Compiled",
    "compile_dir",
    "compile_item",
    "render_jsonl",
    "write_compiled",
]

AUTHORING_DIR = Path("evals") / "datasets" / "authoring" / "v1"
CATEGORY_FILES: dict[str, str] = {
    "code_review": "code_review.jsonl",
    "debugging": "debugging.jsonl",
    "architecture": "architecture.jsonl",
    "planning": "planning.jsonl",
}
REVIEW_REQUEST = (
    "List each problem you find as `path:line` (a line number from the left gutter, which numbers "
    "the new version of each file) followed by the kind of bug and why it is one. Do not list "
    "style nits or speculation. If the change is correct, say so."
)
DEBUG_REQUEST = "Find the root cause of this failure and propose a fix. Rank your hypotheses."
_MARKER = re.compile(r"[ \t]*«([A-Za-z0-9_-]+)»")
_GUTTER = 4


class AuthoringError(ConfigError):
    """An authoring item that cannot be compiled."""


@dataclass
class Compiled:
    tasks: dict[str, list[BenchTask]]  # category -> tasks, in authoring order

    def all(self) -> list[BenchTask]:
        return [t for tasks in self.tasks.values() for t in tasks]


def _need(raw: dict[str, Any], *keys: str) -> None:
    missing = [k for k in keys if k not in raw]
    if missing:
        msg = f"item '{raw.get('id', '?')}' is missing: {', '.join(missing)}"
        raise AuthoringError(msg)


def _common(raw: dict[str, Any], language: str | None) -> dict[str, Any]:
    tags = [*raw.get("tags", []), "synthetic", "llm-authored"]
    if language:
        tags.insert(0, language)
    values: dict[str, Any] = {
        "id": raw["id"],
        "category": raw["category"],
        "difficulty": raw.get("difficulty", "medium"),
        "tags": list(dict.fromkeys(tags)),
    }
    if raw.get("split"):
        values["split"] = raw["split"]
    return values


# -- code review -------------------------------------------------------------------------------


def _parse_diff(item_id: str, path: str, diff: str) -> tuple[list[str], list[tuple[str, str]]]:
    """``(post-change lines, [(op, code)])`` of an authored diff."""
    ops: list[tuple[str, str]] = []
    for number, line in enumerate(diff.rstrip("\n").split("\n"), 1):
        if not line or line[0] not in "=+-":
            msg = f"{item_id} {path} diff line {number} must start with =, + or -: {line!r}"
            raise AuthoringError(msg)
        ops.append((line[0], line[1:]))
    after = [code for op, code in ops if op in "=+"]
    return after, ops


def _render_file(path: str, ops: list[tuple[str, str]]) -> str:
    new = 0
    out = [f"=== {path} ({'new file' if all(o == '+' for o, _ in ops) else 'modified'}) ==="]
    for op, code in ops:
        if op == "-":
            out.append(f"{'':>{_GUTTER}} - {code}")
        else:
            new += 1
            out.append(f"{new:>{_GUTTER}} {'+' if op == '+' else ' '} {code}")
    return "\n".join(out)


def _compile_review(raw: dict[str, Any]) -> BenchTask:
    _need(raw, "id", "title", "description", "files", "language")
    files: dict[str, str] = {}
    sections: list[str] = []
    located: dict[str, tuple[str, int]] = {}
    for entry in raw["files"]:
        path = entry["path"]
        after, ops = _parse_diff(raw["id"], path, entry["diff"])
        clean = []
        new = 0
        cleaned_ops: list[tuple[str, str]] = []
        for op, code in ops:
            marks = _MARKER.findall(code)
            code = _MARKER.sub("", code)
            cleaned_ops.append((op, code))
            if op != "-":
                new += 1
                clean.append(code)
            for mark in marks:
                if op == "-":
                    msg = f"{raw['id']}: bug marker «{mark}» is on a removed line"
                    raise AuthoringError(msg)
                if mark in located:
                    msg = f"{raw['id']}: bug marker «{mark}» appears twice"
                    raise AuthoringError(msg)
                located[mark] = (path, new)
        files[path] = "\n".join(clean) + "\n"
        sections.append(_render_file(path, cleaned_ops))
    bugs = []
    for bug in raw.get("bugs") or []:
        _need(bug, "id", "category", "description")
        if bug["id"] not in located:
            msg = f"{raw['id']}: bug '{bug['id']}' has no «{bug['id']}» marker in any diff"
            raise AuthoringError(msg)
        path, line = located[bug["id"]]
        bugs.append(
            {
                "file": path,
                "line": line,
                "category": bug["category"],
                "severity": bug.get("severity", "medium"),
                "description": bug["description"],
                "aliases": _strings(bug.get("aliases")),
            }
        )
    extra = sorted(set(located) - {b["id"] for b in raw.get("bugs") or []})
    if extra:
        msg = f"{raw['id']}: markers without a bug entry: {', '.join(extra)}"
        raise AuthoringError(msg)
    truth: dict[str, Any] = {"bugs": bugs}
    if "line_tolerance" in raw:
        truth["line_tolerance"] = raw["line_tolerance"]
    return BenchTask.model_validate(
        {
            **_common(raw, raw["language"]),
            "prompt": f"Review this pull request: {raw['title']}\n\n{raw['description'].strip()}"
            f"\n\n{REVIEW_REQUEST}",
            "context": "Diff of the change:\n\n" + "\n\n".join(sections),
            "files": files,
            "truth": truth,
        }
    )


# -- debugging ---------------------------------------------------------------------------------


def _compile_debug(raw: dict[str, Any]) -> BenchTask:
    _need(raw, "id", "symptom", "files", "trace", "root_cause_tags", "language")
    context = f"## Failure output\n\n{raw['trace'].rstrip()}\n"
    if raw.get("logs"):
        context += f"\n## Logs\n\n{raw['logs'].rstrip()}\n"
    truth: dict[str, Any] = {"root_cause_tags": _strings(raw["root_cause_tags"])}
    if raw.get("root_cause_aliases"):
        truth["root_cause_aliases"] = {
            str(tag): _strings(names) for tag, names in raw["root_cause_aliases"].items()
        }
    if raw.get("root_cause"):
        truth["root_cause"] = raw["root_cause"]
    if raw.get("fix_keywords"):
        truth["fix_keywords"] = _strings(raw["fix_keywords"])
    return BenchTask.model_validate(
        {
            **_common(raw, raw["language"]),
            "prompt": f"{raw['symptom'].strip()}\n\n{DEBUG_REQUEST}",
            "context": context,
            "files": {f["path"]: f["content"].rstrip("\n") + "\n" for f in raw["files"]},
            "truth": truth,
        }
    )


# -- architecture and planning -----------------------------------------------------------------


def _strings(values: Any) -> list[str]:
    """A list of strings (YAML reads a bare ``429`` as a number)."""
    return [str(v) for v in values or []]


def _points(items: list[Any] | None) -> list[dict[str, Any]]:
    out = []
    for item in items or []:
        point = {"text": item} if isinstance(item, str) else dict(item)
        if "keywords" in point:
            point["keywords"] = _strings(point["keywords"])
        out.append(point)
    return out


def _compile_rubric(raw: dict[str, Any]) -> BenchTask:
    _need(raw, "id", "prompt", "required")
    truth = {
        "required_points": _points(raw["required"]),
        "forbidden_points": _points(raw.get("forbidden")),
    }
    return BenchTask.model_validate(
        {
            **_common(raw, None),
            "prompt": raw["prompt"].strip(),
            "context": (raw.get("context") or "").strip(),
            "truth": truth,
        }
    )


_COMPILERS = {
    "code_review": _compile_review,
    "debugging": _compile_debug,
    "architecture": _compile_rubric,
    "planning": _compile_rubric,
}


def compile_item(raw: dict[str, Any]) -> BenchTask:
    """One authoring item as a task. Raises ``AuthoringError`` with the item's id on any problem."""
    category = raw.get("category")
    compiler = _COMPILERS.get(str(category))
    if compiler is None:
        msg = f"item '{raw.get('id', '?')}': category must be one of {', '.join(_COMPILERS)}"
        raise AuthoringError(msg)
    try:
        return compiler(raw)
    except AuthoringError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        msg = f"item '{raw.get('id', '?')}': {exc}"
        raise AuthoringError(msg) from exc


def compile_dir(authoring: Path) -> Compiled:
    """Every item of every YAML file in ``authoring``, compiled and grouped by category."""
    if not authoring.is_dir():
        msg = f"Authoring directory {authoring} does not exist"
        raise AuthoringError(msg)
    tasks: dict[str, list[BenchTask]] = {c: [] for c in CATEGORY_FILES}
    for file in sorted(authoring.glob("*.y*ml")):
        loaded = yaml.safe_load(file.read_text(encoding="utf-8")) or []
        if not isinstance(loaded, list):
            msg = f"{file} must hold a list of items"
            raise AuthoringError(msg)
        for raw in loaded:
            try:
                task = compile_item(raw)
            except AuthoringError as exc:
                msg = f"{file.name}: {exc}"
                raise AuthoringError(msg) from exc
            tasks[task.category].append(task)
    return Compiled({c: ts for c, ts in tasks.items() if ts})


def render_jsonl(tasks: list[BenchTask]) -> str:
    """The JSONL text of ``tasks``: one task per line, stable key order, so diffs are readable."""
    rows = [
        json.dumps(t.model_dump(mode="json", exclude_none=True), ensure_ascii=False) for t in tasks
    ]
    return "\n".join(rows) + "\n"


def write_compiled(compiled: Compiled, out: Path) -> list[Path]:
    """Write one ``<category>.jsonl`` per category into ``out``."""
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for category, tasks in compiled.tasks.items():
        path = out / CATEGORY_FILES[category]
        path.write_text(render_jsonl(tasks), encoding="utf-8")
        written.append(path)
    return written


def category_of(name: str) -> Category:
    if name not in CATEGORY_FILES:
        msg = f"category must be one of {', '.join(CATEGORY_FILES)}, not '{name}'"
        raise AuthoringError(msg)
    return name  # type: ignore[return-value]
