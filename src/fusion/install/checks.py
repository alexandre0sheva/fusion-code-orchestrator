"""Read-only checks of each client's Fusion setup: the hook points for ``fusion doctor``.

Nothing here writes a file or starts a process. Each finding is a ``ClientCheck`` with a status and,
when something is off, the command that fixes it; ``fusion doctor`` (roadmap Task 23) decides how to
show them. ``absent`` means the client has no Fusion entry in that place, which is only a fault if
the user means to use that client.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from fusion.install.claude_code import inspect_claude_code
from fusion.install.codex import inspect_codex
from fusion.install.common import ClientCheck
from fusion.install.cursor import inspect_cursor


def inspect_clients(
    *,
    project_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[ClientCheck]:
    """Every client's findings, for the project and the user."""
    return [
        *inspect_claude_code(project_dir=project_dir, environ=environ, home=home),
        *inspect_cursor(project_dir=project_dir, home=home),
        *inspect_codex(project_dir=project_dir, environ=environ, home=home),
    ]
