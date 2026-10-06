"""``fusion install``: one-command setup of the Fusion MCP server in a coding agent."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from fusion.install.claude_code import ClaudeCodeOptions, install_claude_code
from fusion.install.common import InstallError, InstallReport

install_app = typer.Typer(help="Set Fusion up in a coding agent (one command each)")


def show(report: InstallReport) -> None:
    """Print what an install did, line by line."""
    for line in report.actions:
        typer.echo(f"  - {line}")
    for line in report.checks:
        typer.echo(f"  * {line}")
    for line in report.unchanged:
        typer.echo(f"  = {line} (nothing to do)")
    if report.commands:
        typer.echo("\nCommands" + (" it would run:" if report.dry_run else " run:"))
        for command in report.commands:
            typer.echo(f"  {command}")
    if report.notes:
        typer.echo("")
        for note in report.notes:
            typer.echo(f"  {note}")


@install_app.command("claude-code")
def claude_code(
    scope: Annotated[str, typer.Option(help="user (all your projects) or project (.mcp.json)")] = (
        "user"
    ),
    plugin: Annotated[
        bool,
        typer.Option(
            "--plugin", help="Install the plugin (commands, skills, subagent) instead of the server"
        ),
    ] = False,
    ref: Annotated[
        str | None, typer.Option(help="Pin the server to a git tag, branch or commit")
    ] = None,
    local_checkout: Annotated[
        Path | None,
        typer.Option(
            "--local-checkout", help="Run the server from this clone: uv run --directory PATH"
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would happen; change nothing")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Replace a different 'fusion' server already there")
    ] = False,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify", help="Start the server and list its tools before installing"
        ),
    ] = True,
) -> None:
    """Register the Fusion MCP server (or its plugin) with Claude Code. Safe to run again."""
    if scope not in {"user", "project"}:
        raise typer.BadParameter("use user or project", param_hint="--scope")
    options = ClaudeCodeOptions(
        scope="project" if scope == "project" else "user",
        plugin=plugin,
        ref=ref,
        checkout=local_checkout,
        dry_run=dry_run,
        force=force,
        verify=verify,
    )
    try:
        report = install_claude_code(options)
    except InstallError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    show(report)
