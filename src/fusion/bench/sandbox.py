"""A throwaway directory in which benchmark code runs, with limits and no secrets.

``Sandbox`` is how a study runs code that a model wrote (a patch under test, a performance probe)
or that a dataset ships (its hidden tests). It is a context manager over a temporary directory:
``copy_in`` puts files there, ``run`` executes a command in it, ``collect`` reads files back, and
leaving the block deletes everything.

What it enforces, for each command it runs:

* **Wall-clock limit.** The command and everything it started are killed when ``timeout`` passes.
* **Memory, CPU, file size and core dumps**, through ``resource`` limits set by a tiny launcher
  before the command starts. Memory (``RLIMIT_AS``) is honoured on Linux and not on macOS.
* **A scrubbed environment.** Only an allow-list of variables reaches the command (``PATH``, locale)
  plus ones the sandbox sets itself, so API keys and tokens in the parent's environment never do.
  ``HOME`` and ``TMPDIR`` point inside the sandbox.
* **No network and no writes outside the sandbox**, where the platform offers it: ``sandbox-exec``
  on macOS, ``unshare --net`` on Linux. Where it does not, ``isolation`` says ``"none"`` and the
  command runs with only the limits above; ``isolation="require"`` refuses to run then.
* **Paths stay inside.** ``copy_in``, ``write``, ``read`` and ``collect`` reject absolute paths,
  ``..`` and symlinks that leave the sandbox.

This is a best-effort guard for benchmark code from a dataset the owner chose, not a security
boundary against hostile code: see SECURITY.md. It must never be reachable from the MCP server.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Literal

__all__ = [
    "ENV_ALLOWLIST",
    "ISOLATION_ENV",
    "IsolationUnavailableError",
    "RunResult",
    "Sandbox",
    "SandboxError",
    "SandboxLimits",
    "default_isolation",
    "detect_isolation",
    "safe_relative_path",
]

Isolation = Literal["auto", "require", "off"]
Enforced = Literal["sandbox-exec", "unshare", "none"]

ISOLATION_ENV = "FUSION_SANDBOX_ISOLATION"  # auto (default) | require | off
# The only variables of the parent's environment a sandboxed command can see.
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT")
_MAX_PATH_PARTS = 24

_LAUNCHER = """
import json, os, resource, sys
cfg = json.loads(sys.argv[1])
def cap(which, value):
    try:
        resource.setrlimit(which, (value, value))
    except (ValueError, OSError):
        pass
cap(resource.RLIMIT_CORE, 0)
cap(resource.RLIMIT_FSIZE, cfg["file_bytes"])
cap(resource.RLIMIT_CPU, cfg["cpu_s"])
if cfg["mem_bytes"] and sys.platform != "darwin":
    cap(resource.RLIMIT_AS, cfg["mem_bytes"])
os.execvp(sys.argv[2], sys.argv[2:])
"""


class SandboxError(RuntimeError):
    """The sandbox could not do what was asked (a path that escapes it, a missing file)."""


class IsolationUnavailableError(SandboxError):
    """``isolation="require"`` was asked for and this machine cannot provide it."""


@dataclass(frozen=True)
class SandboxLimits:
    timeout_s: float = 30.0
    mem_mb: int | None = 1024
    max_output_bytes: int = 200_000  # of each of stdout and stderr that ``run`` returns
    max_file_mb: int = 64  # the largest file a command may write


@dataclass
class RunResult:
    returncode: int | None  # None: the command was killed for taking too long
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False
    truncated: bool = False  # the output was longer than the limit and was cut

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


@dataclass
class _Probe:
    enforced: Enforced = "none"
    reason: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


_PROBE = _Probe()


def default_isolation() -> Isolation:
    """The isolation mode from ``FUSION_SANDBOX_ISOLATION`` (``auto`` when unset or unknown)."""
    value = os.environ.get(ISOLATION_ENV, "auto").strip().lower()
    return value if value in ("auto", "require", "off") else "auto"  # type: ignore[return-value]


def detect_isolation() -> Enforced:
    """The strongest isolation this machine offers, found once by running a harmless probe."""
    with _PROBE.lock:
        if _PROBE.reason:
            return _PROBE.enforced
        for kind in _candidates():
            if _works(kind):
                _PROBE.enforced = kind
                _PROBE.reason = f"using {kind}"
                break
        else:
            _PROBE.reason = "no network or filesystem isolation is available on this machine"
    return _PROBE.enforced


def _candidates() -> list[Enforced]:
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists():
        return ["sandbox-exec"]
    if sys.platform.startswith("linux") and shutil.which("unshare"):
        return ["unshare"]
    return []


def _works(kind: Enforced) -> bool:
    with tempfile.TemporaryDirectory(prefix="fusion-probe-") as raw:
        root = Path(raw).resolve()
        argv = _wrap(kind, root, [sys.executable, "-c", "print('ok')"])
        try:
            done = subprocess.run(  # noqa: S603 — fixed argv, no shell
                argv, capture_output=True, timeout=15, cwd=root, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0 and b"ok" in done.stdout


def _profile(root: Path) -> str:
    quoted = str(root).replace("\\", "\\\\").replace('"', '\\"')
    return (
        "(version 1)(allow default)(deny network*)(deny file-write*)"
        f'(allow file-write* (subpath "{quoted}") (literal "/dev/null") (literal "/dev/tty") '
        '(literal "/dev/dtracehelper"))'
    )


def _wrap(kind: Enforced, root: Path, argv: list[str]) -> list[str]:
    if kind == "sandbox-exec":
        return ["/usr/bin/sandbox-exec", "-p", _profile(root), *argv]
    if kind == "unshare":
        return ["unshare", "--map-root-user", "--net", "--", *argv]
    return argv


def safe_relative_path(path: str) -> str:
    """``path`` as a clean relative POSIX path; ``SandboxError`` if it could leave the sandbox."""
    if not path or "\x00" in path or "\\" in path:
        msg = f"unsafe path {path!r}"
        raise SandboxError(msg)
    pure = PurePosixPath(path)
    if pure.is_absolute() or path.startswith("~") or (len(path) > 1 and path[1] == ":"):
        msg = f"unsafe path {path!r}: absolute paths are not allowed"
        raise SandboxError(msg)
    parts = [p for p in pure.parts if p != "."]
    if not parts or ".." in parts or len(parts) > _MAX_PATH_PARTS:
        msg = f"unsafe path {path!r}: it must stay inside the sandbox"
        raise SandboxError(msg)
    if ".git" in parts:
        msg = f"unsafe path {path!r}: .git is not allowed"
        raise SandboxError(msg)
    return "/".join(parts)


class Sandbox:
    """A temporary working directory with ``copy_in``, ``run`` and ``collect``. Use ``with``."""

    def __init__(
        self,
        *,
        isolation: Isolation | None = None,
        limits: SandboxLimits | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.mode: Isolation = isolation or default_isolation()
        self.limits = limits or SandboxLimits()
        self._extra_env = dict(env or {})
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._root: Path | None = None
        self.isolation: Enforced = "none"  # what ``run`` enforces; set on entering

    # -- lifecycle --------------------------------------------------------------------------

    def __enter__(self) -> Sandbox:
        if self.mode == "off":
            enforced: Enforced = "none"
        else:
            enforced = detect_isolation()
            if enforced == "none" and self.mode == "require":
                msg = (
                    f"{ISOLATION_ENV}=require but this machine offers no network or filesystem "
                    "isolation (needs sandbox-exec on macOS or unshare on Linux)"
                )
                raise IsolationUnavailableError(msg)
        self.isolation = enforced
        self._tmp = tempfile.TemporaryDirectory(prefix="fusion-sandbox-")
        self._root = Path(self._tmp.name).resolve()
        for name in ("work", "home", "tmp", "io"):
            (self._root / name).mkdir()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
        self._tmp = self._root = None

    @property
    def root(self) -> Path:
        if self._root is None:
            msg = "use the sandbox as a context manager: `with Sandbox() as box:`"
            raise SandboxError(msg)
        return self._root

    @property
    def work(self) -> Path:
        """The directory commands run in; ``copy_in`` and ``collect`` paths are relative to it."""
        return self.root / "work"

    # -- files ------------------------------------------------------------------------------

    def _inside(self, rel: str) -> Path:
        target = self.work / safe_relative_path(rel)
        parent = target.parent.resolve()
        if not (parent == self.work or self.work in parent.parents):
            msg = f"path {rel!r} resolves outside the sandbox"
            raise SandboxError(msg)
        return target

    def write(self, rel: str, data: str | bytes) -> None:
        target = self._inside(rel)
        if target.is_symlink():
            msg = f"path {rel!r} is a symlink"
            raise SandboxError(msg)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)

    def copy_in(self, files: Mapping[str, str | bytes] | Path, *, dest: str = "") -> None:
        """Put ``files`` (path -> content, or a directory's contents) under ``dest``."""
        if isinstance(files, Path):
            base = files
            files = {
                p.relative_to(base).as_posix(): p.read_bytes()
                for p in sorted(base.rglob("*"))
                if p.is_file() and not p.is_symlink()
            }
        for rel, data in files.items():
            self.write(f"{dest}/{rel}" if dest else rel, data)

    def exists(self, rel: str) -> bool:
        return self._inside(rel).exists()

    def remove(self, rel: str) -> None:
        """Delete a file (a patch that deletes one); a missing file is not an error."""
        target = self._inside(rel)
        if target.is_file() or target.is_symlink():
            target.unlink()

    def read(self, rel: str) -> bytes:
        target = self._inside(rel)
        real = Path(os.path.realpath(target))
        if not (self.work in real.parents or real == self.work) or not real.is_file():
            msg = f"no such file in the sandbox: {rel!r}"
            raise SandboxError(msg)
        return real.read_bytes()

    def collect(self, paths: Sequence[str]) -> dict[str, bytes]:
        """The files at ``paths`` (a file, or a directory to take whole), keyed by path. Missing
        paths are left out; a symlink that leaves the sandbox is never followed."""
        found: dict[str, bytes] = {}
        for rel in paths:
            start = self._inside(rel)
            candidates = [start, *sorted(start.rglob("*"))] if start.is_dir() else [start]
            for item in candidates:
                real = Path(os.path.realpath(item))
                inside = real == self.work or self.work in real.parents
                if inside and real.is_file():
                    found[item.relative_to(self.work).as_posix()] = real.read_bytes()
        return found

    # -- running ----------------------------------------------------------------------------

    def _environment(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
        env.update(
            HOME=str(self.root / "home"),
            TMPDIR=str(self.root / "tmp"),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONHASHSEED="0",
            PYTHONUNBUFFERED="1",
            PYTHONNOUSERSITE="1",
        )
        env.update(self._extra_env)
        env.update(extra or {})
        return env

    def run(
        self,
        cmd: Sequence[str],
        *,
        timeout: float | None = None,
        mem_mb: int | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Run ``cmd`` in the work directory. ``python`` means the interpreter running Fusion.

        A command that fails or is killed is a result, not an exception; ``SandboxError`` is for
        misuse (an empty command).
        """
        if not cmd:
            msg = "run needs a command"
            raise SandboxError(msg)
        limit_s = self.limits.timeout_s if timeout is None else timeout
        mem = self.limits.mem_mb if mem_mb is None else mem_mb
        argv = [sys.executable if cmd[0] in ("python", "python3") else cmd[0], *cmd[1:]]
        settings = json.dumps(
            {
                "file_bytes": self.limits.max_file_mb * 1024 * 1024,
                "cpu_s": max(int(limit_s) + 5, 5),
                "mem_bytes": mem * 1024 * 1024 if mem else 0,
            }
        )
        launched = [sys.executable, "-c", _LAUNCHER, settings, *argv]
        full = _wrap(self.isolation, self.root, launched)
        out_path, err_path = self.root / "io" / "stdout", self.root / "io" / "stderr"
        started = time.monotonic()
        timed_out = False
        with out_path.open("wb") as out, err_path.open("wb") as err:
            try:
                proc = subprocess.Popen(  # noqa: S603 — argv list, no shell, scrubbed env
                    full,
                    cwd=self.work,
                    env=self._environment(env),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
            except OSError as exc:
                return RunResult(None, "", f"could not start {argv[0]}: {exc}", 0.0)
            try:
                proc.wait(timeout=limit_s)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)  # the command and anything it started
            proc.wait()
        seconds = time.monotonic() - started
        stdout, cut_out = _read_capped(out_path, self.limits.max_output_bytes)
        stderr, cut_err = _read_capped(err_path, self.limits.max_output_bytes)
        return RunResult(
            returncode=None if timed_out else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            seconds=seconds,
            timed_out=timed_out,
            truncated=cut_out or cut_err,
        )


def _read_capped(path: Path, limit: int) -> tuple[str, bool]:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    return data[:limit].decode("utf-8", errors="replace"), len(data) > limit
