"""SQLite schema migrations."""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 3

_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        task_type TEXT NOT NULL,
        status TEXT NOT NULL,
        input_json TEXT NOT NULL,
        sanitized_input_json TEXT NOT NULL,
        output_json TEXT,
        trace_json TEXT,
        total_cost_usd REAL DEFAULT 0,
        total_latency_ms REAL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS run_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        step_name TEXT NOT NULL,
        model_name TEXT,
        provider TEXT,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0,
        latency_ms REAL DEFAULT 0,
        eval_json TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_runs_task_type ON runs(task_type);
    CREATE INDEX IF NOT EXISTS idx_run_steps_run_id ON run_steps(run_id);
    """,
    2: """
    ALTER TABLE runs ADD COLUMN routing_json TEXT;
    ALTER TABLE runs ADD COLUMN warnings_json TEXT;
    """,
    3: """
    CREATE TABLE IF NOT EXISTS shadow_comparisons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        task_type TEXT,
        baseline_model TEXT NOT NULL,
        judge_model TEXT,
        winner TEXT NOT NULL,
        fusion_score REAL,
        baseline_score REAL,
        fusion_cost_usd REAL,
        baseline_cost_usd REAL,
        fusion_latency_ms REAL,
        baseline_latency_ms REAL,
        raw_json TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_shadow_run_id ON shadow_comparisons(run_id);
    """,
}


def migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] is not None else 0

    for version in sorted(v for v in _MIGRATIONS if v > current):
        conn.executescript(_MIGRATIONS[version])
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.commit()
