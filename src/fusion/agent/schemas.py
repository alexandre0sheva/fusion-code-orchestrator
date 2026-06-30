"""Schemas for coding agent runs."""

from __future__ import annotations

from pydantic import BaseModel, Field

from fusion.orchestration.schemas import CostLatencyInfo


class AgentUsage(BaseModel):
    """Token and cost totals for an agent run."""

    model_alias: str
    model_id: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    agent_steps: int = 0


class AgentRunResult(BaseModel):
    """Result from a single coding agent execution."""

    summary: str
    files_changed: list[str] = Field(default_factory=list)
    usage: AgentUsage
    steps_log: list[str] = Field(default_factory=list)
    verify_exit_code: int | None = None
    verify_output: str = ""
    error: str | None = None


class CompareArmResult(BaseModel):
    """One arm of an implementation comparison."""

    arm: str
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    usage: AgentUsage
    orchestration: CostLatencyInfo | None = None
    orchestration_run_id: str | None = None
    verify_exit_code: int | None = None
    verify_output: str = ""
    workspace_copy: str = ""
    error: str | None = None


class CompareImplementOutput(BaseModel):
    """Side-by-side Opus vs Fusion implementation benchmark."""

    task: str
    workspace_root: str
    opus: CompareArmResult
    fusion: CompareArmResult
    cost_delta_usd: float
    latency_delta_ms: float
    cheaper_arm: str
    faster_arm: str
    notes: list[str] = Field(default_factory=list)
