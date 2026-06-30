"""Orchestration trace structures."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class StepTrace(BaseModel):
    """Trace entry for a single pipeline step."""

    step_name: str
    model_name: str | None = None
    provider: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    eval_summary: dict[str, Any] = Field(default_factory=dict)


class OrchestrationTrace(BaseModel):
    """Full trace of an orchestration run."""

    run_id: str
    task_type: str
    steps: list[StepTrace] = Field(default_factory=list)
    panel_models: list[str] = Field(default_factory=list)
    disagreement_score: float = 0.0
    total_cost_usd: float = 0.0
    total_latency_ms: float = 0.0

    def add_step(self, step: StepTrace) -> None:
        self.steps.append(step)
        self.total_cost_usd += step.cost_usd
        self.total_latency_ms += step.latency_ms
