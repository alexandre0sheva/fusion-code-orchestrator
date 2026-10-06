"""The ``fusion`` command: the root app, its global options, and the commands that need no module.

The rest lives next to what it does: ``run_cmds`` (ask, review-diff, debug, decide, plan,
eval-answer), ``runs_cmds``, ``config_cmds``, ``doctor``, ``models_cmd``, ``fusion.bench.cli`` and
``fusion.install.cli``. ``fusion.cli.app`` re-exports ``app`` for older imports.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from typer.core import TyperGroup
from typer.exceptions import TyperException

from fusion.bench.cli import bench_app
from fusion.cli import dashboard_cmd, doctor, run_cmds, runs_cmds
from fusion.cli.common import (
    EXIT_ERROR,
    CliError,
    DbPathOption,
    JsonOption,
    console,
    echo_json,
    err_console,
)
from fusion.cli.config_cmds import config_app, strategies_app
from fusion.cli.models_cmd import models_app
from fusion.cli.runs_cmds import runs_app
from fusion.config import paths as fusion_paths
from fusion.config.env import load_env
from fusion.config.layers import ConfigError, parse_scalar, set_cli_overrides
from fusion.install.cli import install_app
from fusion.install.common import InstallError
from fusion.storage.run_store import RunStore

# Raised to leave the command line or to report a usage error; Typer shows and exits for these
# itself, so they pass through untouched. (Typer carries its own copy of Click, so `click`'s
# classes would not match.)
_CLICK_CONTROL = (typer.Exit, typer.Abort, TyperException)


class _FusionGroup(TyperGroup):
    """Show a failure as one message and an exit code, not a traceback.

    Configuration and install problems, our own ``CliError`` and any other exception become
    ``Error: ...`` on stderr with exit code 1; ``--verbose`` raises the original instead, so a bug
    can still be reported with its traceback.
    """

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except _CLICK_CONTROL:
            raise
        except CliError as exc:
            _show_error(str(exc), exc.hint)
            raise typer.Exit(exc.code) from None
        except (ConfigError, InstallError) as exc:
            if ctx.params.get("verbose"):
                raise
            _show_error(str(exc), "")
            raise typer.Exit(EXIT_ERROR) from None
        except Exception as exc:
            if ctx.params.get("verbose"):
                raise
            _show_error(
                str(exc) or type(exc).__name__,
                "Run again with --verbose for the full traceback.",
            )
            raise typer.Exit(EXIT_ERROR) from None


def _show_error(message: str, hint: str) -> None:
    err_console.print(
        f"Error: {message}", style="red", soft_wrap=True, highlight=False, markup=False
    )
    if hint:
        err_console.print(hint, style="dim", soft_wrap=True, highlight=False, markup=False)


app = typer.Typer(
    name="fusion",
    help="Fusion Code Orchestrator: a panel of cheap models for review, debugging and planning.",
    no_args_is_help=True,
    cls=_FusionGroup,
)
app.add_typer(runs_app, name="runs")
app.add_typer(config_app, name="config")
app.add_typer(strategies_app, name="strategies")
app.add_typer(models_app, name="models")
app.add_typer(bench_app, name="bench")
app.add_typer(install_app, name="install")
run_cmds.register(app)
doctor.register(app)
dashboard_cmd.register(app)

load_env()


@app.callback()
def _main(
    overrides: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Override a config key for this run, e.g. --set fanout.max_concurrency=2",
        ),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show tracebacks instead of one-line errors")
    ] = False,
) -> None:
    del verbose  # read from the context by the group when something fails
    parsed: dict[str, Any] = {}
    for item in overrides or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            msg = f"--set expects KEY=VALUE (for example fanout.max_concurrency=2), got {item!r}"
            raise typer.BadParameter(msg)
        parsed[key.strip()] = parse_scalar(value)
    set_cli_overrides(parsed)


@app.command("stats")
def stats(
    db_path: DbPathOption = None,
    as_json: JsonOption = False,
    recent_shadow: Annotated[int, typer.Option(help="Recent shadow comparisons to show")] = 10,
) -> None:
    """Show cumulative Fusion stats: spend vs baseline, savings, shadow win-rate."""
    from fusion.telemetry.stats_format import format_stats_markdown, stats_to_dict

    store = RunStore(db_path=db_path)
    fusion_stats = store.get_stats()
    recent = store.list_shadow_comparisons(limit=recent_shadow)
    if as_json:
        echo_json(stats_to_dict(fusion_stats, recent))
        return
    console.print(format_stats_markdown(fusion_stats, recent))


@app.command("init")
def init(
    force: Annotated[bool, typer.Option(help="Overwrite an existing user config")] = False,
    import_legacy: Annotated[
        bool,
        typer.Option(
            "--import-legacy",
            help="Copy a v0.1.0 ./fusion_runs.db into the user data directory",
        ),
    ] = False,
    legacy_path: Annotated[
        Path | None, typer.Option(help="Legacy database to import (default: ./fusion_runs.db)")
    ] = None,
) -> None:
    """Create a commented starter user config and show the next steps."""
    from fusion.storage.legacy import LegacyImportError, import_legacy_db

    target = fusion_paths.user_config_file()
    if target.exists() and not force:
        typer.echo(f"{target} already exists; leaving it alone (use --force to replace it).")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        template = Path(__file__).resolve().parent.parent / "config" / "starter_config.yaml"
        target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        typer.echo(f"Wrote starter config: {target}")

    if import_legacy:
        try:
            result = import_legacy_db(legacy_path or fusion_paths.legacy_db_path())
        except LegacyImportError as exc:
            raise CliError(str(exc)) from None
        plural = "" if result.runs == 1 else "s"
        typer.echo(
            f"Imported {result.runs} run{plural} into {result.destination}; "
            f"{result.source} was left in place."
        )

    typer.echo(
        "\nNext steps:\n"
        "  1. Set provider keys (environment or a .env file): ANTHROPIC_API_KEY, "
        "OPENAI_API_KEY, GOOGLE_API_KEY\n"
        "  2. Check the setup:  fusion doctor\n"
        '  3. Ask something:  fusion ask "How should I retry a failed HTTP call?"\n'
        "  4. See the effective configuration:  fusion config show --resolved\n"
        f"Runs are stored in {fusion_paths.resolve_db_path()}"
    )


@app.command()
def mcp(
    db_path: DbPathOption = None,
    transport: Annotated[
        str, typer.Option(help="stdio (default, spawned by the client) or http (streamable HTTP)")
    ] = "stdio",
    host: Annotated[str, typer.Option(help="HTTP bind address; loopback unless --allow-remote")] = (
        "127.0.0.1"
    ),
    port: Annotated[int, typer.Option(help="HTTP port")] = 8765,
    allow_remote: Annotated[
        bool, typer.Option("--allow-remote", help="Allow a non-loopback --host (no authentication)")
    ] = False,
) -> None:
    """Start the MCP server (stdio by default, or streamable HTTP on localhost)."""
    from fusion.mcp_server.server import run_server

    if transport not in {"stdio", "http"}:
        raise typer.BadParameter("use stdio or http", param_hint="--transport")
    if transport == "stdio" and sys.stdin.isatty():
        err = Console(stderr=True)
        err.print(
            "[yellow]Fusion MCP uses stdin/stdout for JSON-RPC — not an interactive shell.[/yellow]"
        )
        err.print(
            "[dim]Add this server in Cursor or Claude Code MCP settings "
            "(`fusion install cursor`, `fusion install claude-code`); "
            "do not press Enter here.[/dim]"
        )
        err.print("[dim]To check your setup: fusion doctor[/dim]")

    try:
        run_server(
            db_path=db_path,
            transport="http" if transport == "http" else "stdio",
            host=host,
            port=port,
            allow_remote=allow_remote,
        )
    except ValueError as exc:
        Console(stderr=True).print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc


@app.command()
def version(as_json: JsonOption = False) -> None:
    """Show version."""
    from fusion import __version__

    if as_json:
        echo_json({"name": "fusion-code-orchestrator", "version": __version__})
        return
    console.print(f"fusion-code-orchestrator v{__version__}")


run_cmds.register_legacy(app)
runs_cmds.register_legacy(app)

if __name__ == "__main__":
    app()
