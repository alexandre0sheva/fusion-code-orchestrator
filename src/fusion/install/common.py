"""What every installer shares: the server command, safe config edits and a launch check.

stdout belongs to the user here (these are CLI commands, not the MCP server), but nothing in this
module prints: installers return an ``InstallReport`` and the command line shows it.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_URL = "https://github.com/alexandre0sheva/fusion-code-orchestrator"
REPO_SLUG = "alexandre0sheva/fusion-code-orchestrator"
SERVER_NAME = "fusion"
# Tools every Fusion server must list; the launch check looks for the first one.
REQUIRED_TOOL = "fusion_ask"
# Fusion needs Python 3.12. Without this, uvx uses the machine's default Python (often older) and
# the server dies at start-up where nobody sees it; a uv-managed Python is also immune to a broken
# interpreter earlier on PATH.
UVX_PYTHON = ("--python", ">=3.12", "--managed-python")
VERIFY_TIMEOUT_S = 180.0  # the first uvx run clones and builds the package


class InstallError(Exception):
    """An install that cannot continue; the message says what to do about it."""


@dataclass(frozen=True)
class ServerSpec:
    """The command a client runs to start the Fusion MCP server (stdio)."""

    command: str
    args: tuple[str, ...]

    def as_entry(self) -> dict[str, Any]:
        """The server's entry in an ``mcpServers`` map (Claude Code and Cursor share the shape)."""
        return {"command": self.command, "args": list(self.args)}

    def matches(self, entry: object) -> bool:
        """True when a stored entry starts this same command (extra keys such as ``type`` are
        ignored)."""
        return (
            isinstance(entry, dict)
            and entry.get("command") == self.command
            and list(entry.get("args", [])) == list(self.args)
        )

    def shell(self) -> str:
        """The command as one line a person could paste."""
        return " ".join(shell_quote(part) for part in (self.command, *self.args))


def shell_quote(part: str) -> str:
    """One word of a pasteable command line."""
    safe = part and all(c.isalnum() or c in "-_./:@=+~" for c in part)
    return part if safe else "'" + part.replace("'", "'\\''") + "'"


def uvx_spec(ref: str | None = None) -> ServerSpec:
    """Run Fusion straight from GitHub with uvx: no clone, no PyPI release needed.

    ``ref`` pins a tag, branch or commit (``git+URL@ref``). A uv-managed Python 3.12 or newer is
    used (downloaded once if needed).
    """
    source = f"git+{REPO_URL}" + (f"@{ref}" if ref else "")
    return ServerSpec("uvx", (*UVX_PYTHON, "--from", source, "fusion", "mcp"))


def checkout_spec(path: Path) -> ServerSpec:
    """Run Fusion from a local clone: ``uv run --directory PATH fusion mcp``."""
    root = path.expanduser().resolve()
    if not (root / "pyproject.toml").is_file():
        msg = f"{root} is not a Fusion checkout (no pyproject.toml there)."
        raise InstallError(msg)
    return ServerSpec("uv", ("run", "--directory", str(root), "fusion", "mcp"))


@dataclass
class InstallReport:
    """What an install did, or in a dry run would do."""

    dry_run: bool = False
    actions: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)  # what was verified; never a change
    unchanged: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.actions)


# -- JSON config files ----------------------------------------------------------------------


def read_json_object(path: Path) -> dict[str, Any]:
    """A JSON config file as a dict; a missing file is empty and a malformed one is refused."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as exc:
        msg = f"{path} is not valid JSON ({exc}); fix or remove it, then run the install again."
        raise InstallError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{path} does not hold a JSON object; fix it, then run the install again."
        raise InstallError(msg)
    return data


def merge_server(
    path: Path,
    spec: ServerSpec,
    *,
    name: str = SERVER_NAME,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """Add the server to an ``mcpServers`` config file without touching anything else in it.

    Returns ``"added"``, ``"unchanged"`` or ``"replaced"``. A different entry under the same name
    is refused unless ``force`` (the user may have configured it on purpose). The file is
    replaced atomically.
    """
    data = read_json_object(path)
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        msg = f"{path}: 'mcpServers' is not an object; fix it, then run the install again."
        raise InstallError(msg)
    current = servers.get(name)
    if current is not None and spec.matches(current):
        return "unchanged"
    if current is not None and not force:
        msg = (
            f"{path} already has a different '{name}' server ({_describe(current)}). "
            "Run again with --force to replace it."
        )
        raise InstallError(msg)
    outcome = "added" if current is None else "replaced"
    if not dry_run:
        servers[name] = {**(current if isinstance(current, dict) else {}), **spec.as_entry()}
        data["mcpServers"] = servers
        write_json_atomic(path, data)
    return outcome


def _describe(entry: object) -> str:
    if isinstance(entry, dict) and "command" in entry:
        return " ".join([str(entry["command"]), *map(str, entry.get("args", []))])
    return json.dumps(entry)


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write JSON next to the target and move it into place, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        if path.exists():
            os.chmod(temp_name, path.stat().st_mode & 0o777)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


# -- the launch check -------------------------------------------------------------------------


def verify_server(spec: ServerSpec, *, timeout: float = VERIFY_TIMEOUT_S) -> list[str]:
    """Start the server as a client would, list its tools and stop it. Returns the tool names.

    The server runs on the mock provider, so the check needs no API keys and spends nothing.
    """
    try:
        return asyncio.run(asyncio.wait_for(_list_tools(spec), timeout))
    except InstallError:
        raise
    except TimeoutError:
        msg = f"The server did not answer within {timeout:.0f}s: {spec.shell()}"
        raise InstallError(msg) from None
    except Exception as exc:
        msg = f"Could not start the server with `{spec.shell()}`: {exc}"
        raise InstallError(msg) from exc


async def _list_tools(spec: ServerSpec) -> list[str]:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    env = {**os.environ, "FUSION_DEFAULT_PROVIDER": "mock"}
    with tempfile.TemporaryDirectory() as scratch:
        log = Path(scratch) / "server.log"  # the server's stderr: shown only when the check fails
        transport = StdioTransport(
            command=spec.command, args=list(spec.args), env=env, log_file=log
        )
        try:
            async with Client(transport) as client:
                names = sorted(tool.name for tool in await client.list_tools())
        except Exception as exc:
            tail = (
                log.read_text(encoding="utf-8", errors="replace").strip()[-800:]
                if log.exists()
                else ""
            )
            msg = f"Could not start the server with `{spec.shell()}`: {exc}"
            msg += f"\nServer output:\n{tail}" if tail else ""
            raise InstallError(msg) from exc
    if REQUIRED_TOOL not in names:
        msg = f"The server started but does not list {REQUIRED_TOOL} (it listed: {names})."
        raise InstallError(msg)
    return names
