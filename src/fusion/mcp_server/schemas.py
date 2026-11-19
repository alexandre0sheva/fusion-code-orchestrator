"""Pydantic schemas for MCP tool inputs and outputs."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fusion.evals.schemas import ContextEvalResult, FinalEvalResult, ModelResponseEval

STRATEGY_DESCRIPTION = (
    "Strategy name; default panel-nano-pick. auto escalates only when the answer is not enough, "
    "solo-haiku is quickest; panel-digest skips the merge, so you are the aggregator: keep what "
    "several models agree on and check the rest. All: fusion://strategies"
)


DETAIL_DESCRIPTION = (
    "compact (default; about 1.5k tokens, the rest at fusion://runs/{run_id}) or full"
)
MAX_COST_DESCRIPTION = (
    "Hard cost cap in USD; a strategy that cannot fit is shifted to a cheaper form, and if none "
    "fits no model is called"
)
CONTEXT_DESCRIPTION = (
    "Background the panel cannot see (what the code does, constraints), not whole files"
)
SNIPPETS_DESCRIPTION = "Short code excerpts, each prefixed with its file path"
PATHS_DESCRIPTION = (
    "Files for the panel to read, relative to the project root. Read-only; secret, ignored, "
    "binary and oversize files are skipped (the warnings say why)"
)
GLOBS_DESCRIPTION = "Glob patterns of files to read, such as 'src/**/*.py' (at most 200 files)"
DIFF_REF_DESCRIPTION = (
    "A git revision or range whose diff Fusion reads itself, such as 'HEAD~1', 'main...HEAD' or "
    "'HEAD' for uncommitted work"
)
SHADOW_DESCRIPTION = "Force or suppress a shadow A/B run against the baseline model (costs extra)"
BUDGET_DESCRIPTION = "Budget preset; ignored when strategy is set"


def _compact_schema(schema: dict[str, Any], model: type[BaseModel]) -> None:
    """Make a tool's JSON schema cheap for the host, which pays for it on every turn.

    Fields in the model's ``HIDDEN_FROM_SCHEMA`` stay accepted but are not advertised (they are
    documented in docs/INTEGRATIONS.md); ``anyOf: [X, null]`` becomes ``X``; defaults a model
    would assume anyway (null, empty, false) and the class docstring are dropped.
    """
    hidden: frozenset[str] = getattr(model, "HIDDEN_FROM_SCHEMA", frozenset())
    properties: dict[str, Any] = schema.get("properties", {})
    for name in hidden:
        properties.pop(name, None)
    required = [r for r in schema.get("required", []) if r not in hidden]
    if required:
        schema["required"] = required
    for prop in properties.values():
        options = prop.get("anyOf")
        if options and len(options) == 2 and {"type": "null"} in options:
            kept = next(o for o in options if o != {"type": "null"})
            del prop["anyOf"]
            prop.update(kept)
        if prop.get("default") in (None, "", [], {}, False):
            prop.pop("default", None)
    schema.pop("description", None)
    schema.pop("title", None)


class _ToolInput(BaseModel):
    """Base of every tool input: its schema is compacted for the host (``_compact_schema``)."""

    model_config = ConfigDict(json_schema_extra=_compact_schema)
    HIDDEN_FROM_SCHEMA: ClassVar[frozenset[str]] = frozenset()


class _OrchestrationInput(_ToolInput):
    """Inputs shared by every tool that runs the panel.

    ``context`` is the one place for background text. Older clients sent it as ``repo_context``,
    ``repo_summary`` or ``code_context`` depending on the tool; those names are still accepted
    (see ``CONTEXT_ALIASES``) and folded into ``context``, but they are not in the schema.
    """

    CONTEXT_ALIASES: ClassVar[tuple[str, ...]] = ()
    HIDDEN_FROM_SCHEMA: ClassVar[frozenset[str]] = frozenset(
        {"file_snippets", "budget", "shadow_baseline"}
    )

    context: str = Field(default="", description=CONTEXT_DESCRIPTION)
    file_snippets: list[str] = Field(default_factory=list, description=SNIPPETS_DESCRIPTION)
    paths: list[str] = Field(default_factory=list, description=PATHS_DESCRIPTION)
    globs: list[str] = Field(default_factory=list, description=GLOBS_DESCRIPTION)
    diff_ref: str | None = Field(default=None, description=DIFF_REF_DESCRIPTION)
    budget: Literal["low", "medium", "high", "local_only"] = Field(
        default="medium", description=BUDGET_DESCRIPTION
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

    HIDDEN_FROM_SCHEMA: ClassVar[frozenset[str]] = _OrchestrationInput.HIDDEN_FROM_SCHEMA | {
        "changed_files",
        "max_models",
        "include_raw_outputs",
    }

    CONTEXT_ALIASES: ClassVar[tuple[str, ...]] = ("repo_context", "repo_summary")

    diff: str = Field(
        default="",
        description="The unified diff to review; leave empty and pass `diff_ref` instead",
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

    HIDDEN_FROM_SCHEMA: ClassVar[frozenset[str]] = _OrchestrationInput.HIDDEN_FROM_SCHEMA | {
        "changed_files",
        "max_models",
        "include_raw_outputs",
    }

    prompt: str = Field(description="The coding question or task, stated so it stands alone")
    changed_files: list[str] = Field(default_factory=list, description="Relevant file paths")
    max_models: int | None = Field(default=None, description="Maximum panel models to use")
    include_raw_outputs: bool = Field(default=False, description="Include each panel answer")


class DebugErrorInput(_OrchestrationInput):
    """Input for fusion_debug_error tool."""

    HIDDEN_FROM_SCHEMA: ClassVar[frozenset[str]] = _OrchestrationInput.HIDDEN_FROM_SCHEMA | {
        "recent_changes",
        "environment",
    }

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


class PlanInput(_OrchestrationInput):
    """Input for fusion_plan: a feature plan or an architecture decision, by ``kind``."""

    kind: Literal["feature", "decision"] = Field(
        default="feature",
        description="feature: an implementation plan; decision: weigh design options and pick one",
    )
    request: str = Field(
        min_length=1, description="The feature to implement, or the decision to make"
    )
    options: list[str] = Field(
        default_factory=list, description="Options under consideration (decision)"
    )
    constraints: str = Field(default="", description="Constraints and requirements")
    existing_patterns: str = Field(default="", description="Patterns to follow (feature)")

    def _shared(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in _OrchestrationInput.model_fields}

    def as_feature(self) -> PlanFeatureInput:
        shared = self._shared()
        if self.options:  # not lost: the panel sees them as background
            options = "Options under consideration: " + "; ".join(self.options)
            shared["context"] = _background(shared["context"], options)
        return PlanFeatureInput(
            feature_description=self.request,
            constraints=self.constraints,
            existing_patterns=self.existing_patterns,
            **shared,
        )

    def as_decision(self) -> DecideArchitectureInput:
        shared = self._shared()
        if self.existing_patterns.strip():
            patterns = "Existing patterns: " + self.existing_patterns
            shared["context"] = _background(shared["context"], patterns)
        return DecideArchitectureInput(
            question=self.request, options=self.options, constraints=self.constraints, **shared
        )


def _background(context: str, extra: str) -> str:
    return f"{context}\n\n{extra}" if context.strip() else extra


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


VERIFY_DESCRIPTION = (
    "Where the checks run. sandbox (default): in a temporary copy of the project, no network, "
    "deleted afterwards; your project is only read. host: nothing runs; you get the top two "
    "candidates and their checks to run, then call fusion_select_patch. none: no checks. A "
    "sandbox that is off or unavailable falls back to host (the warnings say so)"
)
TESTS_CMD_DESCRIPTION = (
    "Shell command that runs the project's tests from its root, e.g. 'python -m pytest -q "
    "tests/test_parser.py'. Sandbox mode runs it only in the copy (no network) and every "
    "candidate must pass it. Omit when there are no tests"
)
Cell = Literal["pass", "fail", "error", "timeout"]


class ProposePatchInput(_OrchestrationInput):
    """Input for fusion_propose_patch: candidate patches, cross-checked, one returned as text."""

    task: str = Field(description="The change to make, stated so it stands alone")
    tests_cmd: str | None = Field(
        default=None, min_length=1, max_length=2000, description=TESTS_CMD_DESCRIPTION
    )
    verify: Literal["sandbox", "host", "none"] = Field(
        default="sandbox", description=VERIFY_DESCRIPTION
    )
    strategy: str | None = Field(
        default=None,
        description="Strategy name; default verified-synth (verify=none: panel-nano-select)",
    )

    @model_validator(mode="after")
    def _needs_code(self) -> ProposePatchInput:
        if not self.reads_workspace and not self.file_snippets:
            msg = "name the code to change: `paths`, `globs` or `diff_ref` (or `file_snippets`)"
            raise ValueError(msg)
        return self


class SelectPatchInput(_ToolInput):
    """Input for fusion_select_patch: the host's check results for a host-mode proposal."""

    run_id: str = Field(description="The run_id of the host-mode fusion_propose_patch response")
    results: dict[str, dict[str, Cell]] = Field(
        min_length=1,
        description=(
            "{candidate id: {check id: pass|fail|error|timeout}} with that candidate applied. "
            "Add your own test command's result under a new check id such as 'tests'"
        ),
    )
    baseline: dict[str, Cell] = Field(
        default_factory=dict,
        description="Each check's result on the code as it is; checks that cannot tell are dropped",
    )


class FeedbackInput(_ToolInput):
    """Input for fusion_feedback: what became of one run's answer."""

    run_id: str = Field(description="The run_id of the Fusion response the feedback is about")
    outcome: Literal["used", "partly", "wrong", "ignored"] = Field(
        description=(
            "used: you acted on the answer as given; partly: you used some of it; wrong: it was "
            "wrong or misleading; ignored: you did not use it"
        )
    )
    note: str = Field(
        default="",
        max_length=2000,
        description=(
            "One or two sentences on why: what was wrong or missing, or what made it useful. "
            "The library learns from this, so name the kind of mistake, not secrets"
        ),
    )


class EvalAnswerInput(_ToolInput):
    """Input for fusion_eval_answer tool."""

    HIDDEN_FROM_SCHEMA: ClassVar[frozenset[str]] = frozenset({"expected_criteria"})

    answer: str = Field(description="Answer to evaluate")
    question: str = Field(default="", description="Original question or task")
    context: str = Field(default="", description="Context used to generate the answer")
    expected_criteria: list[str] = Field(default_factory=list)
    rubric: str = Field(default="", description="Evaluation rubric")
    detail: Literal["compact", "full"] = Field(default="compact", description=DETAIL_DESCRIPTION)


class FusionStatsInput(_ToolInput):
    """Input for fusion_stats tool."""

    recent_shadow_limit: int = Field(
        default=10,
        description="How many recent shadow A/B comparisons to include",
    )


class CompareClaudeRunsInput(_ToolInput):
    """Input for comparing Claude Code + Opus vs Claude Code + Fusion outputs."""

    HIDDEN_FROM_SCHEMA: ClassVar[frozenset[str]] = frozenset(
        {
            "opus_label",
            "fusion_label",
            "opus_run_id",
            "fusion_run_id",
            "opus_latency_ms",
            "fusion_latency_ms",
            "include_raw_evals",
        }
    )

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
    lessons_used: list[str] | None = Field(
        default=None,
        description=(
            "Ids of the experience library's lessons the panel prompt carried (library.enabled); "
            "send fusion_feedback on this run so they keep or lose their standing"
        ),
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


class PatchToolResult(ToolEnvelope):
    """Result of fusion_propose_patch. The patch is text: Fusion never applies it."""

    run_id: str = Field(description="Handle for the stored run: resource fusion://runs/{run_id}")
    verify: Literal["sandbox", "host", "none"] = Field(
        description="Where the checks ran: sandbox (by Fusion, in a copy), host (by you) or none"
    )
    patch: str | None = Field(
        default=None,
        description=(
            "The chosen patch as a unified diff against the project as it is (sandbox, none); "
            "null in host mode (see candidates) or when no candidate patch applied"
        ),
    )
    files: list[str] = Field(default_factory=list, description="Files the patch changes")
    apply: str = Field(default="", description="How to apply the patch, or what to do next")
    winner: str | None = Field(default=None, description="The panel member whose patch this is")
    reason: str = Field(default="", description="Why this patch was chosen")
    checks: list[dict[str, Any]] = Field(
        default_factory=list,
        description="The checks used: id, author, kind, language, body and, in sandbox mode, the "
        "winner's result",
    )
    matrix: dict[str, dict[str, str]] | None = Field(
        default=None, description="sandbox: each candidate's result on each check"
    )
    candidates: list[dict[str, Any]] | None = Field(
        default=None, description="host: the top two candidate patches (id, patch, files)"
    )
    isolation: str | None = Field(
        default=None, description="sandbox: the isolation used (sandbox-exec or unshare)"
    )
    confidence: float | None = Field(
        default=None,
        description="sandbox: the share of the checks (and your tests) the returned patch passes",
    )
    strategy: str | None = Field(default=None, description="The strategy that produced it")
    cost_usd: float | None = Field(default=None, description="Model spend; null if unknown")
    latency_s: float = Field(default=0.0, description="Wall time, checks included, in seconds")
    models_called: int = Field(default=0, description="Successful model calls")
    details_uri: str = Field(description="Where the full record of this run can be read")
    host_tokens_saved_estimate: int | None = Field(
        default=None, description="Tokens you did not send because Fusion read the files itself"
    )
    lessons_used: list[str] | None = Field(
        default=None, description="Ids of the experience library's lessons the prompt carried"
    )


class SelectPatchResult(ToolEnvelope):
    """Result of fusion_select_patch. No model is called."""

    run_id: str
    winner: str | None = Field(default=None, description="The candidate picked")
    patch: str | None = Field(default=None, description="Its unified diff; never applied")
    reason: str = Field(default="", description="Why it was picked")
    scores: dict[str, float] = Field(default_factory=dict, description="Each candidate's score")
    apply: str = Field(default="", description="How to apply the patch")


class FeedbackResult(ToolEnvelope):
    """Result of fusion_feedback."""

    run_id: str
    recorded: bool = Field(description="False when the experience library is off or refused")
    action: Literal["created", "updated", "dropped", "none"] = Field(
        default="none", description="What the feedback did to the project's lessons"
    )
    lesson: dict[str, Any] | None = Field(
        default=None, description="The lesson created or updated: cue, lesson, verdict, evidence"
    )
    cost_usd: float | None = Field(
        default=None, description="What distilling the lesson cost; null if unknown"
    )


class StatsToolResult(ToolEnvelope):
    """Result of fusion_stats."""

    result: dict[str, Any] = Field(description="The cumulative numbers behind the summary")


class CompareToolResult(ToolEnvelope):
    """Result of fusion_compare_claude_runs."""

    result: dict[str, Any] = Field(description="Verdicts, deltas and per-arm evaluations")
    evals: dict[str, Any] = Field(description="Run ids and scores of the two evaluations")
