"""SQLite connection management."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fusion.config.paths import resolve_db_path
from fusion.storage.migrations import migrate

_BUSY_TIMEOUT_MS = 30_000
_INIT_ATTEMPTS = 40


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a WAL-mode connection (creating parent directories) and apply migrations.

    The connection may be shared across threads; callers serialize access with a lock.
    """
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    _initialise(conn)
    return conn


def _initialise(conn: sqlite3.Connection) -> None:
    """Switch to WAL and migrate, retrying when another process is doing the same.

    SQLite reports SQLITE_BUSY immediately (ignoring busy_timeout) when two connections race to
    convert a fresh database to WAL, so first-open needs its own short retry loop.
    """
    for attempt in range(_INIT_ATTEMPTS):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            migrate(conn)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt + 1 == _INIT_ATTEMPTS:
                raise
            time.sleep(0.02 * (attempt + 1))
