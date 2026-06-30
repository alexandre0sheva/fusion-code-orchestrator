"""Evaluation result schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ContextEvalResult(BaseModel):
    """Result of evaluating whether input context is sufficient."""

    sufficient: bool
    score: float = Field(ge=0.0, le=1.0, description="Context sufficiency score 0-1")
    missing_items: list[str] = Field(default_factory=list)
    notes: str = ""


class ModelResponseEval(BaseModel):
    """Evaluation scores for a single model response."""

    model_name: str
    specificity: float = Field(ge=0.0, le=1.0)
    groundedness: float = Field(ge=0.0, le=1.0)
    actionability: float = Field(ge=0.0, le=1.0)
    correctness_likelihood: float = Field(ge=0.0, le=1.0)
    risk_awareness: float = Field(ge=0.0, le=1.0)
    unsupported_claims: float = Field(ge=0.0, le=1.0, description="Lower is better")
    codebase_awareness: float = Field(ge=0.0, le=1.0)
    novelty: float = Field(ge=0.0, le=1.0)
    overall_score: float = Field(ge=0.0, le=1.0)
    deterministic_passed: bool = True
    deterministic_issues: list[str] = Field(default_factory=list)
    judge_notes: str = ""


class FinalEvalResult(BaseModel):
    """Evaluation of the final synthesized answer."""

    final_answer_quality: float = Field(ge=0.0, le=1.0)
    claude_code_usefulness: float = Field(ge=0.0, le=1.0)
    implementation_readiness: float = Field(ge=0.0, le=1.0)
    test_plan_quality: float = Field(ge=0.0, le=1.0)
    residual_risk: float = Field(ge=0.0, le=1.0, description="Lower is better")
    confidence: float = Field(ge=0.0, le=1.0)
    overall_score: float = Field(ge=0.0, le=1.0)
    deterministic_passed: bool = True
    deterministic_issues: list[str] = Field(default_factory=list)
    notes: str = ""


class OutcomeEvalResult(BaseModel):
    """Hook for future outcome-based evaluation."""

    outcome_id: str | None = None
    measured: bool = False
    notes: str = "Outcome evaluation not yet implemented"


class EvalDimension(BaseModel):
    """A single scored evaluation dimension with human-readable reason."""

    name: str
    score: float = Field(ge=0.0, le=1.0)
    reason: str = ""


class HybridEvalResult(BaseModel):
    """Combined LLM judge + deterministic evaluation result."""

    dimensions: list[EvalDimension] = Field(default_factory=list)
    aggregate_score: float = Field(default=0.0, ge=0.0, le=1.0)
    llm_judge_used: bool = False
    llm_judge_failed: bool = False
    deterministic_passed: bool = True
    deterministic_issues: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: str = ""
