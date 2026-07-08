"""Pydantic schemas for MCP tool inputs and outputs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from fusion.evals.schemas import ContextEvalResult, FinalEvalResult, ModelResponseEval


class ReviewDiffInput(BaseModel):
    """Input for fusion_review_diff tool."""

    diff: str = Field(description="Git diff or patch to review")
    context: str = Field(default="", description="Additional context about the change")
    file_snippets: list[str] = Field(default_factory=list, description="Related file snippets")
    repo_summary: str = Field(default="", description="Optional repository summary (legacy alias)")
    changed_files: list[str] = Field(default_factory=list, description="List of changed file paths")
    repo_context: str = Field(default="", description="Repository context")
    goals: str = Field(default="", description="Review goals or focus areas")
    budget: str = Field(default="medium", description="Budget level: low, medium, high, local_only")
    max_models: int | None = Field(default=None, description="Maximum panel models to use")
    include_raw_outputs: bool = Field(default=False, description="Include raw panel outputs")
    shadow_baseline: bool | None = Field(
        default=None,
        description=(
            "Force (true) or suppress (false) a shadow A/B run against the real "
            "baseline model; defaults to FUSION_SHADOW_MODE env behavior"
        ),
    )


class FusionAskInput(BaseModel):
    """Input for model-like fusion_ask tool."""

    prompt: str = Field(description="Coding question or task for Fusion to answer")
    context: str = Field(default="", description="Repository or task context")
    file_snippets: list[str] = Field(default_factory=list, description="Relevant file snippets")
    changed_files: list[str] = Field(default_factory=list, description="Relevant file paths")
    budget: str = Field(default="medium", description="Budget level: low, medium, high, local_only")
    max_models: int | None = Field(default=None, description="Maximum panel models to use")
    include_raw_outputs: bool = Field(default=False, description="Include raw panel outputs")
    shadow_baseline: bool | None = Field(
        default=None,
        description=(
            "Force (true) or suppress (false) a shadow A/B run against the real "
            "baseline model; defaults to FUSION_SHADOW_MODE env behavior"
        ),
    )


class DebugErrorInput(BaseModel):
    """Input for fusion_debug_error tool."""

    error_message: str = Field(description="Error message or exception text")
    stack_trace: str = Field(default="", description="Stack trace if available")
    context: str = Field(default="", description="Additional debugging context")
    file_snippets: list[str] = Field(default_factory=list)
    logs: str = Field(default="", description="Relevant log output")
    code_context: str = Field(default="", description="Relevant code snippets")
    recent_changes: str = Field(default="", description="Recent changes that may relate")
    environment: str = Field(default="", description="Runtime environment details")
    budget: str = Field(default="medium", description="Budget level")
    shadow_baseline: bool | None = Field(
        default=None,
        description=(
            "Force (true) or suppress (false) a shadow A/B run against the real "
            "baseline model; defaults to FUSION_SHADOW_MODE env behavior"
        ),
    )


class DecideArchitectureInput(BaseModel):
    """Input for fusion_decide_architecture tool."""

    question: str = Field(description="Architecture decision question")
    options: list[str] = Field(default_factory=list, description="Options under consideration")
    constraints: str = Field(default="", description="Constraints and requirements")
    context: str = Field(default="", description="System context")
    file_snippets: list[str] = Field(default_factory=list)
    budget: str = Field(default="medium", description="Budget level")
    shadow_baseline: bool | None = Field(
        default=None,
        description=(
            "Force (true) or suppress (false) a shadow A/B run against the real "
            "baseline model; defaults to FUSION_SHADOW_MODE env behavior"
        ),
    )


class PlanFeatureInput(BaseModel):
    """Input for fusion_plan_feature tool."""

    feature_description: str = Field(description="Feature to implement")
    context: str = Field(default="", description="Project context")
    constraints: str = Field(default="", description="Constraints and requirements")
    file_snippets: list[str] = Field(default_factory=list)
    existing_patterns: str = Field(default="", description="Existing patterns to follow")
    budget: str = Field(default="medium", description="Budget level")
    shadow_baseline: bool | None = Field(
        default=None,
        description=(
            "Force (true) or suppress (false) a shadow A/B run against the real "
            "baseline model; defaults to FUSION_SHADOW_MODE env behavior"
        ),
    )


class EvalAnswerInput(BaseModel):
    """Input for fusion_eval_answer tool."""

    answer: str = Field(description="Answer to evaluate")
    question: str = Field(default="", description="Original question or task")
    context: str = Field(default="", description="Context used to generate the answer")
    expected_criteria: list[str] = Field(default_factory=list)
    rubric: str = Field(default="", description="Evaluation rubric")


class FusionStatsInput(BaseModel):
    """Input for fusion_stats tool."""

    recent_shadow_limit: int = Field(
        default=10,
        description="How many recent shadow A/B comparisons to include",
    )


class CompareImplementInput(BaseModel):
    """Input for fusion_compare_implement benchmark tool."""

    task: str = Field(description="Implementation task to run in both Opus and Fusion arms")
    workspace_root: str = Field(
        default="",
        description="Project root (defaults to FUSION_WORKSPACE_ROOT or cwd)",
    )
    constraints: str = Field(default="", description="Constraints for the implementation")
    verify_command: str = Field(
        default="",
        description="Optional shell command to verify both implementations (e.g. uv run pytest -q)",
    )
    max_agent_steps: int = Field(default=40, description="Max agent turns per arm")
    budget: str = Field(
        default="medium",
        description="Fusion orchestration budget: low/medium/high",
    )
    opus_model: str = Field(default="claude-opus", description="Model alias for Opus baseline arm")
    fusion_executor_model: str = Field(
        default="claude-sonnet",
        description="Model alias for Fusion executor agent",
    )


class CompareClaudeRunsInput(BaseModel):
    """Input for comparing Claude Code + Opus vs Claude Code + Fusion outputs."""

    task_prompt: str = Field(description="Original user prompt/task given to both arms")
    opus_output: str = Field(description="Result from Claude Code using Opus/native model")
    fusion_output: str = Field(description="Result from Claude Code using Fusion MCP")
    context: str = Field(
        default="",
        description="Shared repo/task context and verification results",
    )
    rubric: str = Field(
        default="",
        description=(
            "Optional comparison rubric; defaults to correctness, usefulness, safety, "
            "and testability"
        ),
    )
    opus_label: str = Field(default="Claude Code + Opus")
    fusion_label: str = Field(default="Claude Code + Fusion")
    opus_cost_usd: float | None = Field(default=None, description="Optional measured Opus cost")
    fusion_cost_usd: float | None = Field(default=None, description="Optional measured Fusion cost")
    opus_latency_ms: int | None = Field(default=None, description="Optional measured Opus latency")
    fusion_latency_ms: int | None = Field(
        default=None,
        description="Optional measured Fusion latency",
    )
    opus_run_id: str | None = Field(default=None, description="Optional trace/run id for Opus arm")
    fusion_run_id: str | None = Field(default=None, description="Optional Fusion run id")
    include_raw_evals: bool = Field(default=False, description="Include raw per-arm eval details")


class PanelResultOutput(BaseModel):
    """Panel model result in tool output."""

    model_name: str
    content: str
    evaluation: ModelResponseEval


class ToolOutput(BaseModel):
    """Standard structured output from fusion MCP tools (legacy)."""

    run_id: str
    task_type: str
    final_answer: str
    context_eval: ContextEvalResult
    panel_results: list[PanelResultOutput]
    final_eval: FinalEvalResult
    disagreement: dict[str, Any]
    total_cost_usd: float
    total_latency_ms: float

    @classmethod
    def from_pipeline_result(cls, result: Any) -> ToolOutput:
        """Build ToolOutput from PipelineResult."""
        return cls(
            run_id=result.run_id,
            task_type=result.task_type,
            final_answer=result.final_answer,
            context_eval=result.context_eval,
            panel_results=[
                PanelResultOutput(
                    model_name=p.model_name,
                    content=p.content,
                    evaluation=p.evaluation,
                )
                for p in result.panel_results
            ],
            final_eval=result.final_eval,
            disagreement=result.disagreement,
            total_cost_usd=result.total_cost_usd,
            total_latency_ms=result.total_latency_ms,
        )
