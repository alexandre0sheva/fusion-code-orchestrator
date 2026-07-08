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
class ShadowComparisonRecord:
    """One stored shadow A/B comparison against the baseline model."""

    run_id: str
    baseline_model: str
    winner: str
    task_type: str | None = None
    judge_model: str | None = None
    fusion_score: float | None = None
    baseline_score: float | None = None
    fusion_cost_usd: float | None = None
    baseline_cost_usd: float | None = None
    fusion_latency_ms: float | None = None
    baseline_latency_ms: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass
class TaskTypeStats:
    """Aggregate stats for one task type."""

    task_type: str
    runs: int = 0
    total_cost_usd: float = 0.0
    avg_latency_ms: float = 0.0


@dataclass
class FusionStats:
    """Cumulative statistics across all stored runs."""

    total_runs: int = 0
    completed_runs: int = 0
    total_fusion_cost_usd: float = 0.0
    total_baseline_estimated_cost_usd: float = 0.0
    baseline_estimate_runs: int = 0
    avg_latency_ms: float = 0.0
    by_task_type: list[TaskTypeStats] = field(default_factory=list)
    shadow_total: int = 0
    shadow_fusion_wins: int = 0
    shadow_baseline_wins: int = 0
    shadow_ties: int = 0
    shadow_fusion_cost_usd: float = 0.0
    shadow_baseline_cost_usd: float = 0.0
    shadow_avg_fusion_score: float | None = None
    shadow_avg_baseline_score: float | None = None
    shadow_avg_fusion_latency_ms: float | None = None
    shadow_avg_baseline_latency_ms: float | None = None

    @property
    def shadow_win_rate_percent(self) -> float | None:
        decided = self.shadow_fusion_wins + self.shadow_baseline_wins + self.shadow_ties
        if decided == 0:
            return None
        return (self.shadow_fusion_wins + 0.5 * self.shadow_ties) / decided * 100

    @property
    def estimated_savings_usd(self) -> float:
        return self.total_baseline_estimated_cost_usd - self.total_fusion_cost_usd

    @property
    def estimated_savings_percent(self) -> float | None:
        if self.total_baseline_estimated_cost_usd <= 0:
            return None
        return self.estimated_savings_usd / self.total_baseline_estimated_cost_usd * 100


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

    def record_shadow_comparison(self, record: ShadowComparisonRecord) -> None:
        """Persist one shadow A/B comparison row."""
        conn = get_connection(self._db_path)
        try:
            conn.execute(
                """
                INSERT INTO shadow_comparisons
                (run_id, task_type, baseline_model, judge_model, winner,
                 fusion_score, baseline_score, fusion_cost_usd, baseline_cost_usd,
                 fusion_latency_ms, baseline_latency_ms, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.task_type,
                    record.baseline_model,
                    record.judge_model,
                    record.winner,
                    record.fusion_score,
                    record.baseline_score,
                    record.fusion_cost_usd,
                    record.baseline_cost_usd,
                    record.fusion_latency_ms,
                    record.baseline_latency_ms,
                    json.dumps(record.raw),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def list_shadow_comparisons(self, limit: int = 50) -> list[ShadowComparisonRecord]:
        """Return recent shadow comparisons, newest first."""
        conn = get_connection(self._db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM shadow_comparisons ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [
                ShadowComparisonRecord(
                    run_id=row["run_id"],
                    task_type=row["task_type"],
                    baseline_model=row["baseline_model"],
                    judge_model=row["judge_model"],
                    winner=row["winner"],
                    fusion_score=row["fusion_score"],
                    baseline_score=row["baseline_score"],
                    fusion_cost_usd=row["fusion_cost_usd"],
                    baseline_cost_usd=row["baseline_cost_usd"],
                    fusion_latency_ms=row["fusion_latency_ms"],
                    baseline_latency_ms=row["baseline_latency_ms"],
                    raw=json.loads(row["raw_json"] or "{}"),
                    created_at=row["created_at"],
                )
                for row in rows
            ]
        finally:
            conn.close()

    def get_stats(self) -> FusionStats:
        """Aggregate cumulative statistics across all stored runs."""
        stats = FusionStats()
        conn = get_connection(self._db_path)
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                       COALESCE(SUM(total_cost_usd), 0) AS cost,
                       COALESCE(AVG(total_latency_ms), 0) AS avg_latency
                FROM runs
                """
            ).fetchone()
            stats.total_runs = row["total"] or 0
            stats.completed_runs = row["completed"] or 0
            stats.total_fusion_cost_usd = row["cost"] or 0.0
            stats.avg_latency_ms = row["avg_latency"] or 0.0

            baseline_row = conn.execute(
                """
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(CAST(json_extract(
                           output_json, '$.cost_comparison.baseline_estimated_cost_usd'
                       ) AS REAL)), 0) AS baseline_cost
                FROM runs
                WHERE json_extract(
                    output_json, '$.cost_comparison.baseline_estimated_cost_usd'
                ) IS NOT NULL
                """
            ).fetchone()
            stats.baseline_estimate_runs = baseline_row["n"] or 0
            stats.total_baseline_estimated_cost_usd = baseline_row["baseline_cost"] or 0.0

            task_rows = conn.execute(
                """
                SELECT task_type, COUNT(*) AS runs,
                       COALESCE(SUM(total_cost_usd), 0) AS cost,
                       COALESCE(AVG(total_latency_ms), 0) AS avg_latency
                FROM runs GROUP BY task_type ORDER BY runs DESC
                """
            ).fetchall()
            stats.by_task_type = [
                TaskTypeStats(
                    task_type=task_row["task_type"],
                    runs=task_row["runs"],
                    total_cost_usd=task_row["cost"],
                    avg_latency_ms=task_row["avg_latency"],
                )
                for task_row in task_rows
            ]

            shadow_row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN winner='fusion' THEN 1 ELSE 0 END) AS fusion_wins,
                       SUM(CASE WHEN winner='baseline' THEN 1 ELSE 0 END) AS baseline_wins,
                       SUM(CASE WHEN winner='tie' THEN 1 ELSE 0 END) AS ties,
                       COALESCE(SUM(fusion_cost_usd), 0) AS fusion_cost,
                       COALESCE(SUM(baseline_cost_usd), 0) AS baseline_cost,
                       AVG(fusion_score) AS avg_fusion_score,
                       AVG(baseline_score) AS avg_baseline_score,
                       AVG(fusion_latency_ms) AS avg_fusion_latency,
                       AVG(baseline_latency_ms) AS avg_baseline_latency
                FROM shadow_comparisons
                WHERE winner IN ('fusion', 'baseline', 'tie')
                """
            ).fetchone()
            stats.shadow_total = shadow_row["total"] or 0
            stats.shadow_fusion_wins = shadow_row["fusion_wins"] or 0
            stats.shadow_baseline_wins = shadow_row["baseline_wins"] or 0
            stats.shadow_ties = shadow_row["ties"] or 0
            stats.shadow_fusion_cost_usd = shadow_row["fusion_cost"] or 0.0
            stats.shadow_baseline_cost_usd = shadow_row["baseline_cost"] or 0.0
            stats.shadow_avg_fusion_score = shadow_row["avg_fusion_score"]
            stats.shadow_avg_baseline_score = shadow_row["avg_baseline_score"]
            stats.shadow_avg_fusion_latency_ms = shadow_row["avg_fusion_latency"]
            stats.shadow_avg_baseline_latency_ms = shadow_row["avg_baseline_latency"]
            return stats
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
