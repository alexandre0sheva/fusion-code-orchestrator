"""``fusion dashboard``: serve the local, read-only dashboard."""

from __future__ import annotations

from typing import Annotated

import typer

from fusion.cli.common import CliError, DbPathOption


def dashboard(
    port: Annotated[int, typer.Option(help="Port to listen on (127.0.0.1 only)")] = 8765,
    db_path: DbPathOption = None,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the dashboard in your browser")
    ] = False,
) -> None:
    """Serve a read-only local dashboard: spend vs baseline, runs, benchmarks, configuration.

    It listens on 127.0.0.1 only and never changes your data.
    """
    from fusion.dashboard.app import port_is_free, serve

    if not 1 <= port <= 65535:
        msg = "--port must be between 1 and 65535"
        raise typer.BadParameter(msg, param_hint="--port")
    if not port_is_free(port):
        msg = f"Port {port} is already in use."
        raise CliError(msg, hint=f"Pick another one: fusion dashboard --port {port + 1}")
    typer.echo(f"Fusion dashboard: http://127.0.0.1:{port}/   (Ctrl+C to stop)")
    serve(port=port, db_path=db_path, open_browser=open_browser)


def register(app: typer.Typer) -> None:
    app.command("dashboard")(dashboard)
