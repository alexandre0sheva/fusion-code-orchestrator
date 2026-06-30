"""Workspace path guard for agent file operations."""

from __future__ import annotations

from pathlib import Path


class WorkspaceError(Exception):
    """Raised when an agent operation violates workspace boundaries."""


class WorkspaceGuard:
    """Restricts agent I/O to a single workspace root."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if not self.root.is_dir():
            msg = f"Workspace root does not exist: {self.root}"
            raise WorkspaceError(msg)

    def resolve(self, relative_path: str) -> Path:
        """Resolve a relative path and ensure it stays inside the workspace."""
        cleaned = relative_path.strip() or "."
        candidate = (self.root / cleaned).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            msg = f"Path escapes workspace: {relative_path}"
            raise WorkspaceError(msg)
        return candidate

    def relative(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.root))
