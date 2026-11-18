"""Pydantic schemas for MCP tool inputs and outputs."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from fusion.evals.schemas import ContextEvalResult, FinalEvalResult, ModelResponseEval

STRATEGY_DESCRIPTION = (
    "Strategy name; overrides budget. Read resource fusion://strategies to see them. Examples: "
    "auto (default: estimates the request's difficulty, starts with one cheap model with a "
    "self-check, a cheap panel or a mid panel, and asks more models only when the answer shows "
    "it is not enough; the response's `escalation` says which rung answered), auto-frontier "
    "(auto, allowed to end at Opus 5.5 alone), "
    "solo-haiku (one cheap model), panel-duo (two cheap models; a synthesis call only "
    "when they disagree), "
    "panel-nano (three cheap models), "
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
SNIPPETS_DESCRIPTION = (
    "Short code excerpts, one per item, each prefixed with its file path. Prefer `paths`: "
    "Fusion reads the files itself, so you do not spend tokens pasting them"
)
PATHS_DESCRIPTION = (
    "Files for the panel to read, relative to the project root (the client's first root, else "
    "the server's working directory). Read-only; files outside the root, ignored by "
    ".gitignore, secret (.env, keys), binary or too large are skipped and the warnings say why"
)
GLOBS_DESCRIPTION = (
    "Glob patterns of files to read under the project root, such as 'src/**/*.py'; the same "
    "rules as `paths` apply, and at most 200 files are read"
)
DIFF_REF_DESCRIPTION = (
    "A git revision or range whose diff Fusion reads itself (read-only `git diff <ref>`, no "
    "checkout), such as 'HEAD~1', 'main...HEAD' or a commit sha. For a review it replaces "
    "`diff`; for other tools the diff is added as background"
)
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
    paths: list[str] = Field(default_factory=list, description=PATHS_DESCRIPTION)
    globs: list[str] = Field(default_factory=list, description=GLOBS_DESCRIPTION)
    diff_ref: str | None = Field(default=None, description=DIFF_REF_DESCRIPTION)
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

    @property
    def reads_workspace(self) -> bool:
        """Whether the call names material for Fusion to read from the project."""
        return bool(self.paths or self.globs or self.diff_ref)


class ReviewDiffInput(_OrchestrationInput):
    """Input for fusion_review_diff tool."""

    CONTEXT_ALIASES: ClassVar[tuple[str, ...]] = ("repo_context", "repo_summary")

    diff: str = Field(
        default="",
        description=(
            "The unified diff to review. Leave it empty and pass `diff_ref` instead to have "
            "Fusion read the diff itself"
        ),
    )

    @model_validator(mode="after")
    def _needs_a_diff(self) -> ReviewDiffInput:
        if not self.diff.strip() and not self.diff_ref:
            msg = "give the change to review: `diff_ref` (such as 'HEAD~1') or the `diff` text"
            raise ValueError(msg)
        return self

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


class DigestInput(_OrchestrationInput):
    """Input for fusion_digest: cheap models read large material and return a small answer."""

    question: str = Field(description="What you want to know about the material, standing alone")
    max_answer_tokens: int = Field(
        default=600,
        ge=50,
        le=4000,
        description="Longest answer you want back, in tokens; the response is cut to fit",
    )
    budget: Literal["low", "medium", "high", "local_only"] = Field(
        default="low",
        description="Budget preset; ignored when strategy is set. Default low: one cheap model",
    )

    @model_validator(mode="after")
    def _needs_material(self) -> DigestInput:
        if not self.reads_workspace:
            msg = "name the material to read: `paths`, `globs` or `diff_ref`"
            raise ValueError(msg)
        return self


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
    escalation: str | None = Field(
        default=None,
        description=(
            "strategy auto: the rung of the escalation ladder that answered and why, e.g. "
            "'answered at rung 1 (nano-panel): panel agreed (0.86)'"
        ),
    )
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
    host_tokens_saved_estimate: int | None = Field(
        default=None,
        description="Tokens you did not have to send because Fusion read the files or diff itself",
    )
    # detail="full" only
    workspace: dict[str, Any] | None = Field(
        default=None, description="What was read from the project and what was skipped, and why"
    )
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
