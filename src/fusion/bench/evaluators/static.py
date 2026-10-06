"""``static`` and ``diff_stats``: numbers about the code itself, none of which run the answer.

``StaticEvaluator`` reports, as metrics: lint findings (ruff for Python, eslint for JavaScript and
tsc for TypeScript, each only when installed and named in the task's ``static_tools``; ruff is the
default), type errors (mypy, opt-in), cyclomatic complexity of every Python function (counted from
the AST, so no extra dependency), new third-party imports, secret-looking strings, and, for web
pages, the hints a responsive and offline page leaves in its source (a viewport meta tag, media
queries, the widest fixed width, requests to other hosts). Linters run in the sandbox.
``DiffStatsEvaluator`` counts the lines and files the answer changed against the task's files.
"""

from __future__ import annotations

import ast
import difflib
import json
import re
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

from fusion.bench.evaluators.base import Evidence, Memo, read_tree, run_in_sandbox, tree_digest
from fusion.bench.spec import BenchTask, task_hash
from fusion.bench.virtual import offload
from fusion.security.redaction import _PATTERNS as _REDACTION_PATTERNS

__all__ = ["DiffStatsEvaluator", "StaticEvaluator", "complexity", "web_hints"]

_BRANCHES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ExceptHandler,
    ast.IfExp,
    ast.Assert,
)
_EXTERNAL = re.compile(
    r"""(?:src|href|action)\s*=\s*["'](?:https?:)?//[^"']+|url\(\s*["']?https?://"""
)
_FIXED_WIDTH = re.compile(r"(?<![-\w])(?:min-)?width\s*:\s*(\d+(?:\.\d+)?)px", re.IGNORECASE)
_MEDIA = re.compile(r"@media[^{]*\{")
_VIEWPORT = re.compile(r"<meta[^>]+name=[\"']viewport[\"']", re.IGNORECASE)
DEFAULT_TOOLS = ("ruff",)
# Only credentials with a recognisable shape: the redaction module's looser rules (``password = x``)
# would flag ordinary code, such as ``const password = document.getElementById(...)``.
_SECRET_RULES = frozenset(
    {"aws_key", "github_token", "jwt", "private_key", "sk_key", "anthropic_key"}
)
_SECRETS = [pattern for name, pattern in _REDACTION_PATTERNS if name in _SECRET_RULES]
_LINT_TIMEOUT_S = 60.0


def complexity(source: str) -> list[int]:
    """Cyclomatic complexity of each function in ``source``: one, plus a point for every branch,
    boolean operator, comprehension and match case. A file that does not parse has none."""
    try:
        module = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []
    scores: list[int] = []
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            points = 1
            for inner in _own_nodes(node):
                if isinstance(inner, _BRANCHES):
                    points += 1
                elif isinstance(inner, ast.BoolOp):
                    points += len(inner.values) - 1
                elif isinstance(inner, ast.comprehension):
                    points += 1 + len(inner.ifs)
                elif isinstance(inner, ast.match_case):
                    points += 1
            scores.append(points)
    return scores


def _own_nodes(function: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a function, not descending into functions nested in it (they count alone)."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            stack.extend(ast.iter_child_nodes(node))


def _imports(source: str) -> set[str]:
    try:
        module = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return set()
    found: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def new_dependencies(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Third-party modules the answer imports that the original files did not."""
    local = {Path(p).stem for p in after} | {Path(p).parts[0] for p in after}
    known = set(sys.stdlib_module_names) | local

    def third_party(tree: dict[str, str]) -> set[str]:
        names: set[str] = set()
        for path, text in tree.items():
            if path.endswith(".py"):
                names |= _imports(text) - known
        return names

    added = third_party(after) - third_party(before)
    for path, text in after.items():
        if path.endswith("package.json"):
            added |= _package_deps(text) - _package_deps(before.get(path, "{}"))
    return sorted(added)


def _package_deps(text: str) -> set[str]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return set()
    if not isinstance(data, dict):
        return set()
    return {
        name
        for table in ("dependencies", "devDependencies")
        if isinstance(data.get(table), dict)
        for name in data[table]
    }


def web_hints(tree: dict[str, str]) -> dict[str, float]:
    """What the page's source says about responsiveness and offline use (empty without pages)."""
    pages = {p: t for p, t in tree.items() if p.endswith((".html", ".htm"))}
    styles = {p: t for p, t in tree.items() if p.endswith(".css")}
    if not pages and not styles:
        return {}
    markup = "\n".join(pages.values())
    css = "\n".join(
        [*styles.values(), *re.findall(r"<style[^>]*>(.*?)</style>", markup, re.S | re.I)]
    )
    outside_media = _MEDIA.sub("", css)  # rules inside a media query may legitimately be wide
    widths = [float(m) for m in _FIXED_WIDTH.findall(outside_media)]
    external = len(_EXTERNAL.findall(markup)) + len(_EXTERNAL.findall("\n".join(styles.values())))
    return {
        "html_files": float(len(pages)),
        "css_files": float(len(styles)),
        "has_viewport_meta": 1.0 if any(_VIEWPORT.search(t) for t in pages.values()) else 0.0,
        "media_queries": float(len(_MEDIA.findall(css))),
        "max_fixed_width_px": max(widths, default=0.0),
        "external_requests": float(external),
    }


class StaticEvaluator:
    """Lint, types, complexity, dependencies, secrets and web hints. ``ok`` means: no lint or type
    findings, no secrets, and nothing fetched from another host."""

    name = "static"

    def __init__(self) -> None:
        self._memo = Memo()

    async def run(self, workdir: Path, task: BenchTask) -> Evidence:
        tree = read_tree(workdir)
        key = f"{task_hash(task)}:{tree_digest(tree)}"
        return await offload(lambda: self._memo.get_or_run(key, lambda: self._measure(tree, task)))

    def _measure(self, tree: dict[str, str], task: BenchTask) -> Evidence:
        tools = [str(t) for t in task.truth.get("static_tools", DEFAULT_TOOLS)]
        metrics: dict[str, float] = {}
        notes: list[str] = []
        seconds = 0.0
        py_files = {p: t for p, t in tree.items() if p.endswith(".py")}
        scores = [s for text in py_files.values() for s in complexity(text)]
        if scores:
            metrics["max_complexity"] = float(max(scores))
            metrics["mean_complexity"] = round(sum(scores) / len(scores), 2)
            metrics["functions"] = float(len(scores))
        deps = new_dependencies(task.files, tree)
        metrics["new_dependencies"] = float(len(deps))
        if deps:
            notes.append(f"new dependencies: {', '.join(deps[:5])}")
        secrets = sum(len(p.findall(t)) for t in tree.values() for p in _SECRETS)
        metrics["secrets"] = float(secrets)
        if secrets:
            notes.append(f"{secrets} secret-looking strings")
        metrics.update(web_hints(tree))
        if metrics.get("external_requests"):
            notes.append(f"{int(metrics['external_requests'])} requests to other hosts")
        findings = 0.0
        for tool in tools:
            ran = self._lint(tool, tree, notes)
            if ran is None:
                notes.append(f"{tool} not available")
                continue
            count, took = ran
            seconds += took
            metrics[f"{tool}_issues"] = float(count)
            findings += count
        metrics["lint_issues"] = findings
        ok = findings == 0 and secrets == 0 and not metrics.get("external_requests")
        summary = "; ".join(notes) or "clean"
        return Evidence(
            kind="static", name=self.name, ok=ok, metrics=metrics, summary=summary, seconds=seconds
        )

    def _lint(self, tool: str, tree: dict[str, str], notes: list[str]) -> tuple[int, float] | None:
        """``(findings, seconds)`` of one tool over the tree, or None when it cannot run."""
        ext = {"ruff": ".py", "mypy": ".py", "eslint": ".js", "tsc": ".ts"}.get(tool)
        if ext is None:
            notes.append(f"unknown static tool {tool}")
            return None
        files = sorted(p for p in tree if p.endswith(ext))
        if not files:
            return 0, 0.0
        argv = self._command(tool, files)
        if argv is None:
            return None
        result, _ = run_in_sandbox(tree, argv, timeout_s=_LINT_TIMEOUT_S, mem_mb=None)
        if result.timed_out:
            notes.append(f"{tool} timed out")
            return None
        count = _count_findings(tool, result.stdout, result.stderr, result.returncode)
        if count:
            notes.append(f"{tool}: {count} findings")
        return count, result.seconds

    @staticmethod
    def _command(tool: str, files: list[str]) -> list[str] | None:
        if tool == "ruff":
            exe = shutil.which("ruff")
            base = [exe] if exe else [sys.executable, "-m", "ruff"]
            if not exe and not _module_available("ruff"):
                return None
            return [
                *base,
                "check",
                "--isolated",
                "--no-cache",
                "--output-format",
                "json",
                "--select",
                "E,F,B,SIM",
                "--line-length",
                "100",
                *files,
            ]
        if tool == "mypy":
            if not _module_available("mypy"):
                return None
            return [
                sys.executable,
                "-m",
                "mypy",
                "--ignore-missing-imports",
                "--no-error-summary",
                "--cache-dir",
                "/dev/null",
                *files,
            ]
        exe = shutil.which(tool)
        if exe is None:
            return None
        if tool == "tsc":
            return [exe, "--noEmit", "--strict", *files]
        return [exe, "--no-eslintrc", "--format", "json", *files]


def _module_available(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def _count_findings(tool: str, stdout: str, stderr: str, returncode: int | None) -> int:
    if tool == "ruff":
        try:
            data = json.loads(stdout or "[]")
        except json.JSONDecodeError:
            return 0 if returncode == 0 else 1  # ruff failed to run at all: one finding
        return len(data) if isinstance(data, list) else 0
    if tool == "eslint":
        try:
            data = json.loads(stdout or "[]")
            return sum(len(f.get("messages", [])) for f in data)
        except (json.JSONDecodeError, AttributeError, TypeError):
            return 0 if returncode == 0 else 1
    return len(re.findall(r"\berror\b", stdout + stderr))  # mypy and tsc print one per line


class DiffStatsEvaluator:
    """Lines and files the answer changed against the task's own files."""

    name = "diff_stats"

    async def run(self, workdir: Path, task: BenchTask) -> Evidence:
        tree = read_tree(workdir)
        added = removed = 0
        for path in sorted({*task.files, *tree}):
            old = task.files.get(path, "").splitlines()
            new = tree.get(path, "").splitlines()
            if old == new:
                continue
            for line in difflib.unified_diff(old, new, lineterm="", n=0):
                if line.startswith("+") and not line.startswith("+++"):
                    added += 1
                elif line.startswith("-") and not line.startswith("---"):
                    removed += 1
        changed = [p for p in {*task.files, *tree} if task.files.get(p) != tree.get(p)]
        creations = [p for p in tree if p not in task.files]
        deletions = [p for p in task.files if p not in tree]
        metrics = {
            "lines_added": float(added),
            "lines_removed": float(removed),
            "files_changed": float(len(changed)),
            "files_created": float(len(creations)),
            "files_deleted": float(len(deletions)),
        }
        summary = f"+{added} -{removed} lines in {len(changed)} files"
        return Evidence(
            kind="diff_stats", name=self.name, ok=True, metrics=metrics, summary=summary
        )
