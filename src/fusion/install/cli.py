"""``fusion install``: one-command setup of the Fusion MCP server in a coding agent."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from fusion.install.claude_code import ClaudeCodeOptions, install_claude_code
from fusion.install.codex import CodexOptions, install_codex
from fusion.install.common import InstallError, InstallReport
from fusion.install.cursor import CursorOptions, install_cursor

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


def _finish(install: Callable[[], InstallReport]) -> None:
    """Run an install, show what it did, and turn a refusal into a message and exit code 1."""
    try:
        report = install()
    except InstallError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from None
    show(report)


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
    _finish(lambda: install_claude_code(options))


@install_app.command("cursor")
def cursor(
    global_: Annotated[
        bool,
        typer.Option("--global", help="Register for every project (~/.cursor/mcp.json)"),
    ] = False,
    rules: Annotated[
        bool,
        typer.Option(
            "--rules/--no-rules", help="Also write .cursor/rules/fusion.mdc (project only)"
        ),
    ] = True,
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
        bool,
        typer.Option("--force", help="Replace a different 'fusion' server or an edited rule"),
    ] = False,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify", help="Start the server and list its tools before installing"
        ),
    ] = True,
) -> None:
    """Register the Fusion MCP server with Cursor (.cursor/mcp.json). Safe to run again."""
    options = CursorOptions(
        global_=global_,
        rules=rules,
        ref=ref,
        checkout=local_checkout,
        dry_run=dry_run,
        force=force,
        verify=verify,
    )
    _finish(lambda: install_cursor(options))


@install_app.command("codex")
def codex(
    project: Annotated[
        bool,
        typer.Option("--project", help="Use .codex/config.toml and ./AGENTS.md, not ~/.codex"),
    ] = False,
    agents_md: Annotated[
        bool,
        typer.Option("--agents-md/--no-agents-md", help="Also add the 'when to call Fusion' block"),
    ] = True,
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
        bool, typer.Option("--force", help="Replace the command of a different 'fusion' server")
    ] = False,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify", help="Start the server and list its tools before installing"
        ),
    ] = True,
) -> None:
    """Register the Fusion MCP server with Codex (config.toml). Safe to run again."""
    options = CodexOptions(
        project=project,
        agents_md=agents_md,
        ref=ref,
        checkout=local_checkout,
        dry_run=dry_run,
        force=force,
        verify=verify,
    )
    _finish(lambda: install_codex(options))
