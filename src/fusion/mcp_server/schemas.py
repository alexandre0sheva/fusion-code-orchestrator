"""Pydantic schemas for MCP tool inputs and outputs."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from fusion.evals.schemas import ContextEvalResult, FinalEvalResult, ModelResponseEval

STRATEGY_DESCRIPTION = (
    "Strategy name; overrides budget. Read resource fusion://strategies to see them. Examples: "
    "solo-cheap (one cheap model), panel-duo (default: two cheap models, one synthesis call), "
    "panel-cheap (three cheap models), "
    "panel-cascade (two cheap models, the rest only if they disagree), panel-refine (adds a "
    "refinement round), panel-vote (the points most models backed, no synthesis). "
    "panel-digest returns every panel answer plus the shared, disputed and single-model points "
    "WITHOUT a synthesis call, so you are the aggregator: keep what several models agree on and "
    "check disputed or single-model points against the code before relying on them. A strategy "
    "may carry a hard cost cap (max_cost_usd): the run is then shifted to a cheaper form and "
    "the response says so in its warnings."
)


DETAIL_DESCRIPTION = (
    "compact (default): the answer, the top five claims, confidence and one cost line, about 1.5k "
    "tokens at most; the rest is kept under run_id (resource fusion://runs/{run_id}). "
    "full: every claim, the cost breakdown, all warnings and the structured result"
)
MAX_COST_DESCRIPTION = (
    "Hard cost cap in USD for this call. It can only lower the strategy's own cap. If the "
    "strategy cannot fit, it is shifted to a cheaper form (and the warnings say so); if even the "
    "cheapest form cannot fit, no model is called. Omit for the strategy's default."
)
CONTEXT_DESCRIPTION = (
    "Background the panel needs and cannot see: what the code does, the relevant files or "
    "constraints. More relevant context gives better answers; do not paste whole files here."
)
SNIPPETS_DESCRIPTION = "Short code excerpts, one per item, each prefixed with its file path"
SHADOW_DESCRIPTION = (
    "Force (true) or suppress (false) a shadow A/B run against the real baseline model; "
    "defaults to FUSION_SHADOW_MODE env behavior. A shadow run spends extra money."
)


class _OrchestrationInput(BaseModel):
    """Inputs shared by every tool that runs the panel.

    ``context`` is the one place for background text. Older clients sent it as ``repo_context``,
    ``repo_summary`` or ``code_context`` depending on the tool; those names are still accepted
    (see ``CONTEXT_ALIASES``) and folded into ``context``, but they are not in the schema.
    """

    CONTEXT_ALIASES: ClassVar[tuple[str, ...]] = ()

    context: str = Field(default="", description=CONTEXT_DESCRIPTION)
    file_snippets: list[str] = Field(default_factory=list, description=SNIPPETS_DESCRIPTION)
    budget: Literal["low", "medium", "high", "local_only"] = Field(
        default="medium", description="Budget preset; ignored when strategy is set"
    )
    strategy: str | None = Field(default=None, description=STRATEGY_DESCRIPTION)
    max_cost_usd: float | None = Field(default=None, gt=0, description=MAX_COST_DESCRIPTION)
    detail: Literal["compact", "full"] = Field(default="compact", description=DETAIL_DESCRIPTION)
    shadow_baseline: bool | None = Field(default=None, description=SHADOW_DESCRIPTION)

    @model_validator(mode="before")
    @classmethod
    def _fold_context_aliases(cls, data: Any) -> Any:
        aliases = cls.CONTEXT_ALIASES
        if not isinstance(data, dict) or not any(name in data for name in aliases):
            return data
        texts: list[str] = []
        for name in ("context", *aliases):
            value = data.get(name)
            if isinstance(value, str) and value.strip() and value not in texts:
                texts.append(value)
        folded = {k: v for k, v in data.items() if k not in aliases}
        folded["context"] = "\n\n".join(texts)
        return folded


class ReviewDiffInput(_OrchestrationInput):
    """Input for fusion_review_diff tool."""

    CONTEXT_ALIASES: ClassVar[tuple[str, ...]] = ("repo_context", "repo_summary")

    diff: str = Field(description="The git diff or patch to review, as unified diff text")
    changed_files: list[str] = Field(default_factory=list, description="Changed file paths")
    goals: str = Field(default="", description="What to focus on, such as security or concurrency")
    max_models: int | None = Field(default=None, description="Maximum panel models to use")
    include_raw_outputs: bool = Field(default=False, description="Include each panel answer")


class FusionAskInput(_OrchestrationInput):
    """Input for model-like fusion_ask tool."""

    prompt: str = Field(description="The coding question or task, stated so it stands alone")
    changed_files: list[str] = Field(default_factory=list, description="Relevant file paths")
    max_models: int | None = Field(default=None, description="Maximum panel models to use")
    include_raw_outputs: bool = Field(default=False, description="Include each panel answer")


class DebugErrorInput(_OrchestrationInput):
    """Input for fusion_debug_error tool."""

    CONTEXT_ALIASES: ClassVar[tuple[str, ...]] = ("code_context",)

    error_message: str = Field(description="The error message or exception text")
    stack_trace: str = Field(default="", description="The stack trace, if there is one")
    logs: str = Field(default="", description="Relevant log output")
    recent_changes: str = Field(default="", description="Recent changes that may relate")
    environment: str = Field(default="", description="Runtime environment details")


class DecideArchitectureInput(_OrchestrationInput):
    """Input for fusion_decide_architecture tool."""

    question: str = Field(description="The architecture decision to make")
    options: list[str] = Field(default_factory=list, description="Options under consideration")
    constraints: str = Field(default="", description="Constraints and requirements")


class PlanFeatureInput(_OrchestrationInput):
    """Input for fusion_plan_feature tool."""

    feature_description: str = Field(description="The feature to implement")
    constraints: str = Field(default="", description="Constraints and requirements")
    existing_patterns: str = Field(default="", description="Existing patterns to follow")


class EvalAnswerInput(BaseModel):
    """Input for fusion_eval_answer tool."""

    answer: str = Field(description="Answer to evaluate")
    question: str = Field(default="", description="Original question or task")
    context: str = Field(default="", description="Context used to generate the answer")
    expected_criteria: list[str] = Field(default_factory=list)
    rubric: str = Field(default="", description="Evaluation rubric")
    detail: Literal["compact", "full"] = Field(default="compact", description=DETAIL_DESCRIPTION)


class FusionStatsInput(BaseModel):
    """Input for fusion_stats tool."""

    recent_shadow_limit: int = Field(
        default=10,
        description="How many recent shadow A/B comparisons to include",
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


class ToolEnvelope(BaseModel):
    """What every Fusion tool returns: text for the model to read, plus a typed record."""

    display_markdown: str = Field(description="The answer, ready to read; the model's main input")
    warnings: list[str] = Field(
        default_factory=list,
        description="Caveats: provider failures, cost-cap shifts, soft-timeout notices",
    )


class FusionToolResult(ToolEnvelope):
    """Result of the tools that run the panel (ask, review, debug, decide, plan, eval).

    Compact responses carry the first block of fields; ``detail: full`` adds the second.
    """

    run_id: str = Field(description="Handle for the stored run: resource fusion://runs/{run_id}")
    strategy: str | None = Field(default=None, description="The strategy that produced the answer")
    confidence: float | None = Field(default=None, description="0 to 1, calibrated by agreement")
    cost_usd: float | None = Field(
        default=None, description="Estimated Fusion spend; null if unknown"
    )
    latency_s: float = Field(default=0.0, description="Wall time of the run in seconds")
    models_called: int = Field(default=0, description="Successful model calls")
    partial: bool = Field(
        default=False,
        description="True when the soft time limit cut the run short and this is the panel digest",
    )
    halted: str | None = Field(
        default=None,
        description="Why no answer was produced: insufficient_context, quorum, budget or timeout",
    )
    details_uri: str = Field(description="Where the full record of this run can be read")
    # detail="full" only
    result: dict[str, Any] | None = Field(
        default=None, description="Task-specific structured result"
    )
    claims: list[dict[str, Any]] | None = Field(default=None, description="Claims across the panel")
    agreement: dict[str, Any] | None = Field(default=None, description="Agreement measurements")
    usage: dict[str, Any] | None = Field(
        default=None, description="Per-model tokens, cost, latency"
    )
    cost_comparison: dict[str, Any] | None = Field(default=None, description="Versus the baseline")
    routing: dict[str, Any] | None = Field(default=None, description="Why these models were chosen")
    evals: dict[str, Any] | None = Field(default=None, description="Scoring of each stage")
    raw_outputs: list[dict[str, Any]] | None = Field(
        default=None, description="Each panel answer, when include_raw_outputs is set"
    )


class StatsToolResult(ToolEnvelope):
    """Result of fusion_stats."""

    result: dict[str, Any] = Field(description="The cumulative numbers behind the summary")


class CompareToolResult(ToolEnvelope):
    """Result of fusion_compare_claude_runs."""

    result: dict[str, Any] = Field(description="Verdicts, deltas and per-arm evaluations")
    evals: dict[str, Any] = Field(description="Run ids and scores of the two evaluations")
