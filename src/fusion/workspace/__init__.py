"""Read-only access to the user's project: named files, globs and git diffs (roadmap Task 5)."""

from fusion.workspace.reader import (
    DEFAULT_MAX_BYTES_FILE,
    DEFAULT_MAX_BYTES_TOTAL,
    ProjectCopy,
    ProjectFiles,
    ReadFile,
    Skipped,
    Snippet,
    WorkspaceError,
    WorkspaceReader,
    WorkspaceReport,
)

__all__ = [
    "DEFAULT_MAX_BYTES_FILE",
    "DEFAULT_MAX_BYTES_TOTAL",
    "ProjectCopy",
    "ProjectFiles",
    "ReadFile",
    "Skipped",
    "Snippet",
    "WorkspaceError",
    "WorkspaceReader",
    "WorkspaceReport",
]
