"""Read files and diffs from the user's project so the host does not have to paste them.

The reader only reads. It refuses anything outside the workspace root (``../``, absolute paths
elsewhere, symlinks that lead out), files inside ``.git``, files ``.gitignore`` excludes, secret
files (``security.redaction.SECRET_FILE_PATTERNS``), binaries, and files over the per-file or
per-call byte limits. Each refusal is recorded with its reason; what is read has its secrets
redacted before it goes anywhere.
"""

from __future__ import annotations

import os
import re
import stat
from functools import cache
from pathlib import Path

from pydantic import BaseModel, Field

from fusion.security.redaction import is_secret_file, redact_secrets
from fusion.workspace import git

DEFAULT_MAX_BYTES_FILE = 128_000
# About 80K tokens: under the 100K-token long-context price step of the cheap models.
DEFAULT_MAX_BYTES_TOTAL = 320_000
MAX_GLOB_MATCHES = 200
BYTES_PER_TOKEN = 4
_SNIFF = 8192

MAX_BYTES_ENV = "FUSION_WORKSPACE_MAX_BYTES"
MAX_FILE_BYTES_ENV = "FUSION_WORKSPACE_MAX_FILE_BYTES"


class WorkspaceError(ValueError):
    """A workspace request that cannot be served at all (a bad glob or ref, a failed diff)."""


class Snippet(BaseModel):
    """One file read from the workspace, secrets redacted."""

    path: str = Field(description="Path relative to the workspace root")
    text: str
    bytes: int = Field(description="Size of the file as read, before redaction")
    redactions: int = 0

    def as_file_snippet(self) -> str:
        """The ``file_snippets`` form the pipelines use: the path, a newline, the text."""
        return f"{self.path}\n{self.text}"


class Skipped(BaseModel):
    """A path that was not read, and why."""

    path: str
    reason: str


class ReadFile(BaseModel):
    path: str
    bytes: int
    redactions: int = 0


class WorkspaceReport(BaseModel):
    """What one call read from the workspace and what it refused; stored with the run."""

    root: str
    read: list[ReadFile] = Field(default_factory=list)
    skipped: list[Skipped] = Field(default_factory=list)
    diff_ref: str | None = None
    diff_bytes: int = 0
    bytes_read: int = 0
    host_tokens_saved_estimate: int = Field(
        default=0, description="Bytes Fusion read so the host did not have to send them, / 4"
    )

    def skipped_warning(self) -> str | None:
        """One line naming the skipped paths and why, or None when nothing was skipped."""
        if not self.skipped:
            return None
        shown = "; ".join(f"{s.path}: {s.reason}" for s in self.skipped[:5])
        more = f"; {len(self.skipped) - 5} more" if len(self.skipped) > 5 else ""
        return f"Workspace: not read ({len(self.skipped)}): {shown}{more}."


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@cache
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """``pattern`` as a regex over ``/``-separated paths: ``*`` and ``?`` stay inside one
    directory, ``**`` crosses any number of them."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and (end := pattern.find("]", i + 1)) > i:
            body = pattern[i + 1 : end].replace("\\", "\\\\")
            out.append(f"[^{body[1:]}]" if body.startswith("!") else f"[{body}]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


class WorkspaceReader:
    """Reads files and git diffs under one root for one tool call, within byte limits."""

    def __init__(
        self,
        root: Path,
        max_bytes_total: int = DEFAULT_MAX_BYTES_TOTAL,
        max_bytes_file: int = DEFAULT_MAX_BYTES_FILE,
    ) -> None:
        given = Path(os.path.abspath(root))
        if not given.is_dir():
            msg = f"The workspace root {given} is not a directory"
            raise WorkspaceError(msg)
        self._given = given
        self.root = given.resolve()
        self.max_bytes_total = max_bytes_total
        self.max_bytes_file = max_bytes_file
        self.skipped: list[Skipped] = []
        self._snippets: list[Snippet] = []
        self._seen: set[Path] = set()
        self._bytes = 0
        self._diff_ref: str | None = None
        self._diff_bytes = 0
        self._repo: bool | None = None

    @classmethod
    def from_env(cls, root: Path) -> WorkspaceReader:
        """A reader whose limits come from ``FUSION_WORKSPACE_MAX_BYTES`` and
        ``FUSION_WORKSPACE_MAX_FILE_BYTES`` (bytes), or the defaults."""
        return cls(
            root,
            max_bytes_total=_env_int(MAX_BYTES_ENV, DEFAULT_MAX_BYTES_TOTAL),
            max_bytes_file=_env_int(MAX_FILE_BYTES_ENV, DEFAULT_MAX_BYTES_FILE),
        )

    @property
    def in_repo(self) -> bool:
        if self._repo is None:
            self._repo = git.is_work_tree(self.root)
        return self._repo

    def _skip(self, path: str, reason: str) -> None:
        self.skipped.append(Skipped(path=path, reason=reason))

    # -- paths ---------------------------------------------------------------------------------

    def _locate(self, raw: str) -> tuple[str, Path, str] | str:
        """(display path, resolved path, resolved path relative to the root), or why not."""
        given = Path(raw)
        lexical = Path(os.path.normpath(given if given.is_absolute() else self._given / given))
        base = next((b for b in (self._given, self.root) if lexical.is_relative_to(b)), None)
        resolved = lexical.resolve()
        if not resolved.is_relative_to(self.root):
            if base is not None:
                return "outside the workspace root (a symlink leads out of it)"
            return "outside the workspace root"
        display = lexical.relative_to(base) if base else resolved.relative_to(self.root)
        rel = resolved.relative_to(self.root)
        if ".git" in display.parts or ".git" in rel.parts:
            return "inside .git"
        if is_secret_file(display.as_posix()) or is_secret_file(rel.as_posix()):
            return "secret file (deny list); never read"
        return display.as_posix(), resolved, rel.as_posix()

    def read_paths(self, paths: list[str]) -> list[Snippet]:
        """Read ``paths`` (relative to the root, or absolute inside it) in order; each refusal is
        added to ``skipped``. Returns the files read by this call."""
        located: list[tuple[str, str, Path, str]] = []
        for raw in paths:
            found = self._locate(raw)
            if isinstance(found, str):
                self._skip(raw, found)
            else:
                located.append((raw, *found))
        excluded: set[str] = set()
        if located and self.in_repo:
            try:
                excluded = git.ignored(self.root, [rel for *_, rel in located])
            except git.GitError as exc:
                for raw, *_ in located:
                    self._skip(raw, f"cannot check .gitignore: {exc}")
                return []
        read: list[Snippet] = []
        for raw, display, resolved, rel in located:
            if rel in excluded:
                self._skip(raw, "ignored by .gitignore")
                continue
            if resolved in self._seen:
                continue
            snippet = self._read_one(raw, display, resolved)
            if snippet is not None:
                self._seen.add(resolved)
                read.append(snippet)
        self._snippets.extend(read)
        return read

    def _read_one(self, raw: str, display: str, path: Path) -> Snippet | None:
        try:
            info = path.stat()
        except FileNotFoundError:
            self._skip(raw, "not found")
            return None
        except OSError as exc:
            self._skip(raw, f"cannot read: {exc.strerror or exc}")
            return None
        if stat.S_ISDIR(info.st_mode):
            self._skip(raw, "a directory; name files or use globs")
            return None
        if not stat.S_ISREG(info.st_mode):
            self._skip(raw, "not a regular file")
            return None
        if info.st_size > self.max_bytes_file:
            limit = f"{info.st_size:,} bytes > {self.max_bytes_file:,}"
            self._skip(raw, f"over the per-file limit ({limit})")
            return None
        if self._bytes + info.st_size > self.max_bytes_total:
            self._skip(raw, f"over the total read limit ({self.max_bytes_total:,} bytes per call)")
            return None
        try:
            with path.open("rb") as handle:
                data = handle.read(self.max_bytes_file + 1)
        except OSError as exc:
            self._skip(raw, f"cannot read: {exc.strerror or exc}")
            return None
        if len(data) > self.max_bytes_file:  # it grew after the size check
            self._skip(raw, "over the per-file limit")
            return None
        if b"\0" in data[:_SNIFF]:
            self._skip(raw, "binary file")
            return None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self._skip(raw, "binary file or not UTF-8 text")
            return None
        found = redact_secrets(text)
        self._bytes += len(data)
        return Snippet(
            path=display, text=found.text, bytes=len(data), redactions=found.redaction_count
        )

    # -- globs ---------------------------------------------------------------------------------

    def expand_globs(self, globs: list[str]) -> list[str]:
        """Files under the root that match ``globs`` (``*``, ``?``, ``[..]``, ``**``), as paths
        relative to the root. In a git repository only tracked and untracked-but-not-ignored
        files are candidates, so ignored trees (``node_modules``, ``.venv``) are never walked."""
        for pattern in globs:
            parts = Path(pattern).parts
            if not pattern.strip() or Path(pattern).is_absolute() or ".." in parts:
                msg = f"Globs must name files inside the workspace root: {pattern!r}"
                raise WorkspaceError(msg)
        if not globs:
            return []
        regexes = [_glob_regex(p.removeprefix("./")) for p in globs]
        matches = [f for f in self._candidates() if any(r.match(f) for r in regexes)]
        if len(matches) > MAX_GLOB_MATCHES:
            reason = f"matched {len(matches)} files; only the first {MAX_GLOB_MATCHES} were kept"
            self._skip(", ".join(globs), reason)
            matches = matches[:MAX_GLOB_MATCHES]
        return matches

    def _candidates(self) -> list[str]:
        if self.in_repo:
            try:
                return [f for f in git.listed_files(self.root) if (self.root / f).is_file()]
            except git.GitError as exc:
                raise WorkspaceError(str(exc)) from None
        found: list[str] = []
        for top, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d != ".git")
            base = Path(top).relative_to(self.root)
            found.extend((base / name).as_posix() for name in files)
        return sorted(found)

    # -- git diff ------------------------------------------------------------------------------

    def git_diff(self, ref: str) -> str:
        """``git diff <ref>`` (read-only; no checkout) for the files under the root, secret files
        left out and secrets redacted. It counts against the per-call byte limit."""
        if not git.valid_ref(ref):
            msg = f"Not a git ref or range: {ref!r} (use e.g. HEAD~1, main...HEAD, a commit sha)"
            raise WorkspaceError(msg)
        try:
            names = git.changed_files(self.root, ref)
            secret = [name for name in names if is_secret_file(name)]
            text = git.diff(self.root, ref, exclude=secret) if len(secret) < len(names) else ""
        except git.GitError as exc:
            msg = f"git diff {ref} failed in {self.root}: {exc}"
            raise WorkspaceError(msg) from None
        size = len(text.encode())
        if self._bytes + size > self.max_bytes_total:
            msg = (
                f"The diff for {ref} is {size:,} bytes, over the read limit of "
                f"{self.max_bytes_total:,} bytes per call. Pass a narrower diff_ref, or name the "
                f"files to read in paths ({MAX_BYTES_ENV} raises the limit)."
            )
            raise WorkspaceError(msg)
        for name in secret:
            self._skip(name, "secret file (deny list); left out of the diff")
        self._bytes += size
        self._diff_ref = ref
        self._diff_bytes = size
        return redact_secrets(text).text

    # -- report --------------------------------------------------------------------------------

    def report(self) -> WorkspaceReport:
        return WorkspaceReport(
            root=str(self.root),
            read=[
                ReadFile(path=s.path, bytes=s.bytes, redactions=s.redactions)
                for s in self._snippets
            ],
            skipped=list(self.skipped),
            diff_ref=self._diff_ref,
            diff_bytes=self._diff_bytes,
            bytes_read=self._bytes,
            host_tokens_saved_estimate=self._bytes // BYTES_PER_TOKEN,
        )
