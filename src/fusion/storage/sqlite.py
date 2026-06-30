"""SQLite connection management."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from fusion.storage.migrations import migrate


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection and apply migrations."""
    path = db_path or os.environ.get("FUSION_DB_PATH", "./fusion_runs.db")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn
