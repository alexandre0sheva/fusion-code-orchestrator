"""Persist orchestration run traces to SQLite."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fusion.storage.sqlite import get_connection
from fusion.utils.ids import new_run_id


@dataclass
class RunStepRecord:
    """A single step within an orchestration run."""

    step_name: str
    model_name: str | None = None
    provider: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    eval_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunRecord:
    """Complete record of an orchestration run."""

    run_id: str
    task_type: str
    status: str
    input_data: dict[str, Any]
    sanitized_input: dict[str, Any]
    output_data: dict[str, Any] | None = None
    trace: dict[str, Any] = field(default_factory=dict)
    routing: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    total_cost_usd: float = 0.0
    total_latency_ms: float = 0.0
    steps: list[RunStepRecord] = field(default_factory=list)


class RunStore:
    """SQLite-backed store for orchestration runs."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path

    def create_run(
        self,
        *,
        task_type: str,
        input_data: dict[str, Any],
        sanitized_input: dict[str, Any],
        run_id: str | None = None,
    ) -> str:
        rid = run_id or new_run_id()
        conn = get_connection(self._db_path)
        try:
            conn.execute(
                """
                INSERT INTO runs (run_id, task_type, status, input_json, sanitized_input_json)
                VALUES (?, ?, 'running', ?, ?)
                """,
                (rid, task_type, json.dumps(input_data), json.dumps(sanitized_input)),
            )
            conn.commit()
        finally:
            conn.close()
        return rid

    def complete_run(
        self,
        run_id: str,
        *,
        status: str,
        output_data: dict[str, Any],
        trace: dict[str, Any],
        total_cost_usd: float,
        total_latency_ms: float,
        steps: list[RunStepRecord],
        routing: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        conn = get_connection(self._db_path)
        try:
            conn.execute(
                """
                UPDATE runs SET status=?, output_json=?, trace_json=?,
                routing_json=?, warnings_json=?,
                total_cost_usd=?, total_latency_ms=?
                WHERE run_id=?
                """,
                (
                    status,
                    json.dumps(output_data),
                    json.dumps(trace),
                    json.dumps(routing or {}),
                    json.dumps(warnings or []),
                    total_cost_usd,
                    total_latency_ms,
                    run_id,
                ),
            )
            for step in steps:
                conn.execute(
                    """
                    INSERT INTO run_steps
                    (run_id, step_name, model_name, provider, input_tokens, output_tokens,
                     cost_usd, latency_ms, eval_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        step.step_name,
                        step.model_name,
                        step.provider,
                        step.input_tokens,
                        step.output_tokens,
                        step.cost_usd,
                        step.latency_ms,
                        json.dumps(step.eval_data),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def get_run(self, run_id: str) -> RunRecord | None:
        conn = get_connection(self._db_path)
        try:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            steps_rows = conn.execute(
                "SELECT * FROM run_steps WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
            steps = [
                RunStepRecord(
                    step_name=s["step_name"],
                    model_name=s["model_name"],
                    provider=s["provider"],
                    input_tokens=s["input_tokens"],
                    output_tokens=s["output_tokens"],
                    cost_usd=s["cost_usd"],
                    latency_ms=s["latency_ms"],
                    eval_data=json.loads(s["eval_json"] or "{}"),
                )
                for s in steps_rows
            ]
            keys = row.keys()
            return RunRecord(
                run_id=row["run_id"],
                task_type=row["task_type"],
                status=row["status"],
                input_data=json.loads(row["input_json"]),
                sanitized_input=json.loads(row["sanitized_input_json"]),
                output_data=json.loads(row["output_json"]) if row["output_json"] else None,
                trace=json.loads(row["trace_json"]) if row["trace_json"] else {},
                routing=json.loads(row["routing_json"])
                if "routing_json" in keys and row["routing_json"]
                else {},
                warnings=json.loads(row["warnings_json"])
                if "warnings_json" in keys and row["warnings_json"]
                else [],
                total_cost_usd=row["total_cost_usd"],
                total_latency_ms=row["total_latency_ms"],
                steps=steps,
            )
        finally:
            conn.close()

    def list_runs(self, limit: int = 20) -> list[RunRecord]:
        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(
                "SELECT run_id FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [r for rid in rows if (r := self.get_run(rid["run_id"])) is not None]
        finally:
            conn.close()
