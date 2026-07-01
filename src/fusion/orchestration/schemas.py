"""Pydantic schemas for pipeline inputs and structured outputs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from fusion.evals.schemas import (
    ContextEvalResult,
    FinalEvalResult,
    ModelResponseEval,
    OutcomeEvalResult,
)
from fusion.routing.budget import BudgetLevel
from fusion.routing.policy import RoutingDecision
from fusion.telemetry.cost import CostComparison, UsageSummary


class StepUsage(BaseModel):
    """Token and cost usage for one pipeline step."""

    step_name: str
    model_name: str | None = None
    provider: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0
    cost_known: bool = True
    cost_is_estimate: bool = True
    latency_ms: float = 0.0


class CostLatencyInfo(BaseModel):
    """Cost and latency summary for a pipeline run."""

    total_cost_usd: float | None = 0.0
    total_cost_known: bool = True
    total_latency_ms: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    steps: list[StepUsage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PipelineEvals(BaseModel):
    """Aggregated evaluation results from all pipeline stages."""

    context: ContextEvalResult | None = None
    per_answer: list[ModelResponseEval] = Field(default_factory=list)
    disagreement: dict[str, Any] = Field(default_factory=dict)
    judge_quality: dict[str, Any] | None = None
    final: FinalEvalResult | None = None
    outcome: OutcomeEvalResult | None = None
    aggregate_score: float = Field(default=0.0, ge=0.0, le=1.0)
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class CodeReviewInput(BaseModel):
    """Input for code review pipeline."""

    diff: str
    changed_files: list[str] = Field(default_factory=list)
    repo_context: str = ""
    goals: str = ""
    budget: BudgetLevel = BudgetLevel.MEDIUM
    max_models: int | None = None
    include_raw_outputs: bool = False


class FusionAskInput(BaseModel):
    """Input for general model-like Fusion answers."""

    prompt: str
    context: str = ""
    file_snippets: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    budget: BudgetLevel = BudgetLevel.MEDIUM
    max_models: int | None = None
    include_raw_outputs: bool = False


class FusionAskOutput(BaseModel):
    """Structured output from general Fusion answer pipeline."""

    answer: str
    summary: str = ""
    suggested_actions: list[str] = Field(default_factory=list)
    tests_to_run: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    evals: PipelineEvals
    routing: RoutingDecision
    cost_latency: CostLatencyInfo
    display_markdown: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    usage: UsageSummary | None = None
    cost_comparison: CostComparison | None = None
    warnings: list[str] = Field(default_factory=list)
    run_id: str
    raw_outputs: list[dict[str, Any]] | None = None


class CodeReviewOutput(BaseModel):
    """Structured output from code review pipeline."""

    summary: str
    critical_findings: list[str] = Field(default_factory=list)
    recommended_changes: list[str] = Field(default_factory=list)
    false_positive_risks: list[str] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    consensus: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    unique_insights: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    evals: PipelineEvals
    routing: RoutingDecision
    cost_latency: CostLatencyInfo
    display_markdown: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    usage: UsageSummary | None = None
    cost_comparison: CostComparison | None = None
    warnings: list[str] = Field(default_factory=list)
    run_id: str
    raw_outputs: list[dict[str, Any]] | None = None


class DebugInput(BaseModel):
    """Input for debug pipeline."""

    error_message: str
    logs: str = ""
    code_context: str = ""
    recent_changes: str = ""
    environment: str = ""
    budget: BudgetLevel = BudgetLevel.MEDIUM


class DebugOutput(BaseModel):
    """Structured output from debug pipeline."""

    most_likely_causes: list[str] = Field(default_factory=list)
    ranked_hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)
    minimal_fix_strategy: str = ""
    what_not_to_do: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    evals: PipelineEvals
    cost_latency: CostLatencyInfo
    display_markdown: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    usage: UsageSummary | None = None
    cost_comparison: CostComparison | None = None
    routing: RoutingDecision
    warnings: list[str] = Field(default_factory=list)
    run_id: str


class ArchitectureDecisionInput(BaseModel):
    """Input for architecture decision pipeline."""

    decision_question: str
    constraints: str = ""
    options: list[str] = Field(default_factory=list)
    repo_context: str = ""
    budget: BudgetLevel = BudgetLevel.MEDIUM


class ArchitectureDecisionOutput(BaseModel):
    """Structured output from architecture decision pipeline."""

    recommended_option: str = ""
    tradeoffs: list[str] = Field(default_factory=list)
    rejected_options: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    reversibility: str = ""
    migration_plan: list[str] = Field(default_factory=list)
    test_strategy: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    evals: PipelineEvals
    cost_latency: CostLatencyInfo
    display_markdown: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    usage: UsageSummary | None = None
    cost_comparison: CostComparison | None = None
    routing: RoutingDecision
    warnings: list[str] = Field(default_factory=list)
    run_id: str


class ImplementationPlanInput(BaseModel):
    """Input for implementation plan pipeline."""

    feature_request: str
    constraints: str = ""
    repo_context: str = ""
    existing_patterns: str = ""
    budget: BudgetLevel = BudgetLevel.MEDIUM


class ImplementationPlanOutput(BaseModel):
    """Structured output from implementation plan pipeline."""

    implementation_sequence: list[str] = Field(default_factory=list)
    affected_modules: list[str] = Field(default_factory=list)
    data_model_changes: list[str] = Field(default_factory=list)
    api_changes: list[str] = Field(default_factory=list)
    ui_changes: list[str] = Field(default_factory=list)
    tests_to_add: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    evals: PipelineEvals
    cost_latency: CostLatencyInfo
    display_markdown: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    usage: UsageSummary | None = None
    cost_comparison: CostComparison | None = None
    routing: RoutingDecision
    warnings: list[str] = Field(default_factory=list)
    run_id: str


class AnswerEvalInput(BaseModel):
    """Input for answer evaluation pipeline."""

    question: str
    answer: str
    context: str = ""
    rubric: str = ""


class AnswerEvalOutput(BaseModel):
    """Structured output from answer evaluation pipeline."""

    score: float = Field(ge=0.0, le=1.0)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    missing_points: list[str] = Field(default_factory=list)
    safer_answer: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    evals: PipelineEvals
    cost_latency: CostLatencyInfo
    display_markdown: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    usage: UsageSummary | None = None
    cost_comparison: CostComparison | None = None
    routing: RoutingDecision
    warnings: list[str] = Field(default_factory=list)
    run_id: str
