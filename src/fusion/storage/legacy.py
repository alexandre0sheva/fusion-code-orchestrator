"""One-off import of the v0.1.0 cwd-relative database into the user data directory."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from fusion.config.paths import resolve_db_path
from fusion.storage.sqlite import get_connection


class LegacyImportError(RuntimeError):
    """The legacy database cannot be imported (missing, or the target already has data)."""


@dataclass(frozen=True)
class ImportResult:
    source: Path
    destination: Path
    runs: int
    shadow_comparisons: int


def import_legacy_db(source: Path, destination: Path | None = None) -> ImportResult:
    """Copy ``source`` into ``destination`` (default: the resolved database path).

    The source is opened read-only and never modified. The destination must be empty or absent:
    merging two histories would double-count cost statistics.
    """
    if not source.is_file():
        msg = f"Legacy database not found: {source}"
        raise LegacyImportError(msg)
    target = destination or resolve_db_path()

    existing = get_connection(target)
    try:
        has_runs = existing.execute("SELECT COUNT(*) FROM runs").fetchone()[0] > 0
    finally:
        existing.close()
    if has_runs:
        msg = f"{target} already contains runs; refusing to merge {source} into it"
        raise LegacyImportError(msg)

    try:
        src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        msg = f"Cannot open legacy database {source}: {exc}"
        raise LegacyImportError(msg) from exc
    try:
        dest = sqlite3.connect(str(target))
        try:
            src.backup(dest)
        finally:
            dest.close()
    except sqlite3.Error as exc:
        msg = f"Cannot read legacy database {source}: {exc}"
        raise LegacyImportError(msg) from exc
    finally:
        src.close()

    migrated = get_connection(target)  # upgrades the copied schema and switches it to WAL
    try:
        runs = migrated.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        shadow = migrated.execute("SELECT COUNT(*) FROM shadow_comparisons").fetchone()[0]
    finally:
        migrated.close()
    return ImportResult(source, target, runs, shadow)
