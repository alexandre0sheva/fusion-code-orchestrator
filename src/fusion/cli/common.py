"""What every command shares: exit codes, output, input, shared options and the error contract.

Exit codes (the same for every command):

* ``0`` success (a run that was cut short but produced a digest, ``partial``, still succeeds);
* ``1`` the command failed: a bad configuration, a missing key, an unreadable file, a failed check;
* ``2`` the command line itself is wrong (Click's usage error);
* ``3`` a run finished without an answer (``halted``: no quorum, over budget, timed out, not
  enough context).

Results go to stdout and nothing else does, so ``fusion ... --json | jq`` always parses. Progress,
warnings and errors go to stderr.
"""

from __future__ import annotations

import asyncio
import enum
import json
import os
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
from rich.console import Console

from fusion.cli.live import RunView
from fusion.config.catalog import load_catalog
from fusion.config.env import is_local_provider_enabled
from fusion.mcp_server.tools import FusionTools

T = TypeVar("T")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_HALTED = 3

# Provider -> the environment variable that holds its key.
PROVIDER_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
}

console = Console()
err_console = Console(stderr=True)


class CliError(Exception):
    """A failure the user can act on: shown as one message (and a hint), never a traceback."""

    def __init__(self, message: str, *, hint: str = "", code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.hint = hint
        self.code = code


class Detail(enum.StrEnum):
    compact = "compact"
    full = "full"


StrategyOption = Annotated[
    str | None, typer.Option("--strategy", help="Strategy name (see `fusion strategies list`)")
]
MaxCostOption = Annotated[
    float | None, typer.Option("--max-cost", help="Hard cost cap in USD for this run")
]
DetailOption = Annotated[
    Detail, typer.Option("--detail", help="compact: the answer and top claims; full: everything")
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print the result as JSON on stdout")]
MockOption = Annotated[bool, typer.Option("--mock", help="Use the offline mock provider")]
DbPathOption = Annotated[str | None, typer.Option("--db-path", help="SQLite database path")]
QuietOption = Annotated[
    bool, typer.Option("--quiet", "-q", help="No progress on stderr (the result is still printed)")
]
ContextOption = Annotated[
    str, typer.Option("--context", help="Background the panel cannot see (what the code does)")
]
ContextFileOption = Annotated[
    Path | None, typer.Option("--context-file", help="Read the background from a file")
]


def echo_json(data: object) -> None:
    """Print JSON on stdout: indented, no colour, no wrapping, so a pipe can parse it."""
    typer.echo(json.dumps(data, indent=2, default=str))


def read_file(path: Path, what: str = "file") -> str:
    """The text of a file, or ``-`` for stdin; a missing or binary file is a one-line error."""
    if str(path) == "-":
        return sys.stdin.read()
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        msg = f"{what} not found: {path}"
        raise CliError(msg) from None
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"Cannot read {what} {path}: {exc}"
        raise CliError(msg) from None


def primary_text(value: str | None, file: Path | None, *, what: str, flag: str = "--file") -> str:
    """The command's main input: an argument, ``-`` (stdin) or a file. Missing is a usage error."""
    if file is not None:
        return read_file(file, what)
    if value == "-":
        return sys.stdin.read()
    if value and value.strip():
        return value
    msg = f"give {what} as an argument or with {flag} (use - for stdin)"
    raise CliError(msg, code=EXIT_USAGE)


def context_text(context: str, context_file: Path | None) -> str:
    """``--context`` and ``--context-file`` joined (either may be empty)."""
    parts = [context.strip()] if context.strip() else []
    if context_file is not None:
        parts.append(read_file(context_file, "context file").strip())
    return "\n\n".join(part for part in parts if part)


def check_max_cost(max_cost: float | None) -> float | None:
    if max_cost is not None and max_cost <= 0:
        msg = "--max-cost must be more than 0 (USD)"
        raise CliError(msg, code=EXIT_USAGE)
    return max_cost


def require_a_provider(mock: bool) -> None:
    """Stop with an actionable message when no model could possibly answer.

    One missing key is not an error (the panel works with whichever providers answer); none at
    all is, and a run would only come back halted with the reason buried in its warnings.
    """
    if mock or os.environ.get("FUSION_DEFAULT_PROVIDER", "").strip().lower() == "mock":
        return
    if any(is_local_provider_enabled(name) for name in ("ollama", "lmstudio")):
        return
    wanted = {
        PROVIDER_KEYS[entry.provider]
        for entry in load_catalog().models.values()
        if entry.enabled and entry.provider in PROVIDER_KEYS
    }
    if wanted and not any(os.environ.get(name, "").strip() for name in wanted):
        raise CliError(
            "No provider API key is set, so no model can answer.",
            hint=(
                f"Export {' / '.join(sorted(wanted))} (or put it in a .env file), "
                "run `fusion doctor` to check your setup, or add --mock to try it offline."
            ),
        )


def make_tools(db_path: str | None, mock: bool) -> FusionTools:
    if mock:
        os.environ["FUSION_DEFAULT_PROVIDER"] = "mock"
    return FusionTools(db_path=db_path, use_mock=True if mock else None)


def run_tool(
    tools: FusionTools,
    call: Callable[[FusionTools], Coroutine[Any, Any, T]],
    *,
    quiet: bool = False,
    view_console: Console | None = None,
) -> T:
    """Run one tool call under the live view, then close provider clients on the same loop."""
    view = RunView(console=view_console or err_console, quiet=quiet)

    async def go() -> T:
        try:
            with view:
                return await call(tools)
        finally:
            await tools.aclose()

    return asyncio.run(go())


def deprecated(old: str, new: str) -> None:
    """One-release deprecation notice on stderr; the command still runs."""
    err_console.print(
        f"[yellow]Deprecated:[/yellow] `fusion {old}` will be removed in 0.3.0; "
        f"use `fusion {new}`.",
        soft_wrap=True,
        highlight=False,
    )
