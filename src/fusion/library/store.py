"""Where lessons, run feedback and lesson use are kept: Fusion's own SQLite database in the user
data directory, keyed by a hash of the project root, never inside the project."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from fusion.config.paths import resolve_db_path
from fusion.storage.sqlite import get_connection

Verdict = Literal["helped", "hurt", "no_effect", "confounded"]
Confidence = Literal["high", "medium", "low"]
Outcome = Literal["used", "partly", "wrong", "ignored"]

VERDICTS: tuple[Verdict, ...] = ("helped", "hurt", "no_effect", "confounded")
CONFIDENCES: tuple[Confidence, ...] = ("low", "medium", "high")  # weakest first


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def project_key(root: Path) -> str:
    """A stable, opaque key for a project: the first 16 hex digits of the SHA-256 of its resolved
    root path. The path itself is never stored."""
    resolved = os.path.realpath(Path(root).expanduser())
    return hashlib.sha256(resolved.encode()).hexdigest()[:16]


def inside(path: Path, root: Path) -> bool:
    """Whether ``path`` resolves to ``root`` or somewhere under it."""
    target = Path(os.path.realpath(Path(path).expanduser()))
    base = Path(os.path.realpath(Path(root).expanduser()))
    return target == base or base in target.parents


class Lesson(BaseModel):
    """One evidence-backed lesson: when a situation shaped like ``cue`` comes up, do ``lesson``.

    ``evidence`` holds one line per run that bears on it (``evidence_line``); ``verdict`` sums
    them up: what the behaviour the lesson describes did in those runs.
    """

    id: str
    project_key: str
    task_type: str
    cue: str = Field(min_length=1, max_length=300)
    lesson: str = Field(min_length=1, max_length=600)
    evidence: list[str] = Field(default_factory=list)
    verdict: Verdict
    confidence: Confidence
    created_at: str
    updated_at: str = ""

    @classmethod
    def new(cls, **fields: Any) -> Lesson:
        """A lesson with a fresh id and today's timestamps."""
        now = _now()
        return cls(id=f"L{uuid.uuid4().hex[:8]}", created_at=now, updated_at=now, **fields)


class Feedback(BaseModel):
    """What the host said about one run's answer."""

    run_id: str
    project_key: str
    task_type: str
    outcome: Outcome
    note: str = ""
    lesson_id: str | None = None  # the lesson this feedback created or updated
    created_at: str = Field(default_factory=_now)


class LibraryStore:
    """Lessons, feedback and lesson use in the run database (``db_path``, else
    ``FUSION_DB_PATH``, else the user data directory).

    One connection, opened lazily and guarded by a lock, like ``RunStore``.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.path = resolve_db_path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = get_connection(self.path)
        return self._conn

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
            if conn is not None:
                conn.close()

    def _write(self, sql: str, params: tuple[Any, ...]) -> int:
        with self._lock:
            conn = self._db()
            try:
                count = conn.execute(sql, params).rowcount
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return count

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db().execute(sql, params).fetchall())

    # -- lessons ---------------------------------------------------------------------------

    def save(self, lesson: Lesson) -> None:
        """Insert or replace one lesson."""
        self._write(
            """
            INSERT OR REPLACE INTO lessons
            (id, project_key, task_type, cue, lesson, evidence_json, verdict, confidence,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lesson.id,
                lesson.project_key,
                lesson.task_type,
                lesson.cue,
                lesson.lesson,
                json.dumps(lesson.evidence),
                lesson.verdict,
                lesson.confidence,
                lesson.created_at,
                lesson.updated_at or lesson.created_at,
            ),
        )

    def get(self, lesson_id: str) -> Lesson | None:
        rows = self._rows("SELECT * FROM lessons WHERE id=?", (lesson_id,))
        return _lesson(rows[0]) if rows else None

    def lessons(self, project_key: str | None = None, task_type: str | None = None) -> list[Lesson]:
        """Lessons, newest first; all projects' when ``project_key`` is None."""
        sql, params = "SELECT * FROM lessons WHERE 1=1", []
        if project_key is not None:
            sql += " AND project_key=?"
            params.append(project_key)
        if task_type is not None:
            sql += " AND task_type=?"
            params.append(task_type)
        sql += " ORDER BY updated_at DESC, rowid DESC"
        return [_lesson(row) for row in self._rows(sql, tuple(params))]

    def forget(self, lesson_id: str) -> bool:
        """Delete one lesson and its use records; False when there was none."""
        found = self._write("DELETE FROM lessons WHERE id=?", (lesson_id,)) > 0
        self._write("DELETE FROM lesson_uses WHERE lesson_id=?", (lesson_id,))
        return found

    def forget_project(self, project_key: str) -> int:
        """Delete every lesson of one project, and its feedback; returns the lessons deleted."""
        ids = [lesson.id for lesson in self.lessons(project_key)]
        for lesson_id in ids:
            self.forget(lesson_id)
        self._write("DELETE FROM run_feedback WHERE project_key=?", (project_key,))
        return len(ids)

    # -- feedback and use ------------------------------------------------------------------

    def record_feedback(self, feedback: Feedback) -> None:
        """Store the host's feedback on a run; a second call for the same run replaces it."""
        self._write(
            """
            INSERT OR REPLACE INTO run_feedback
            (run_id, project_key, task_type, outcome, note, lesson_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feedback.run_id,
                feedback.project_key,
                feedback.task_type,
                feedback.outcome,
                feedback.note,
                feedback.lesson_id,
                feedback.created_at,
            ),
        )

    def feedback(self, run_id: str) -> Feedback | None:
        rows = self._rows("SELECT * FROM run_feedback WHERE run_id=?", (run_id,))
        return Feedback.model_validate(dict(rows[0])) if rows else None

    def record_uses(self, run_id: str, lesson_ids: list[str]) -> None:
        """Remember which lessons a run's panel prompt carried, in prompt order."""
        with self._lock:
            conn = self._db()
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO lesson_uses (run_id, lesson_id, position) "
                    "VALUES (?, ?, ?)",
                    [(run_id, lesson_id, i) for i, lesson_id in enumerate(lesson_ids)],
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def lessons_used(self, run_id: str) -> list[str]:
        rows = self._rows(
            "SELECT lesson_id FROM lesson_uses WHERE run_id=? ORDER BY position", (run_id,)
        )
        return [row["lesson_id"] for row in rows]

    def runs_using(self, lesson_id: str) -> list[str]:
        rows = self._rows(
            "SELECT run_id FROM lesson_uses WHERE lesson_id=? ORDER BY rowid", (lesson_id,)
        )
        return [row["run_id"] for row in rows]


def _lesson(row: sqlite3.Row) -> Lesson:
    data = dict(row)
    data["evidence"] = json.loads(data.pop("evidence_json") or "[]")
    return Lesson.model_validate(data)
