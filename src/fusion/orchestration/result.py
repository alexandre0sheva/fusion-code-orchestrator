"""Pipeline result types and the usage summary derived from the run ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fusion.benchmark.shadow import ShadowComparison
from fusion.evals.schemas import ContextEvalResult, FinalEvalResult, ModelResponseEval
from fusion.orchestration.budget_guard import BudgetReport
from fusion.orchestration.cascade import CascadeOutcome
from fusion.orchestration.claims import AgreementReport, ClaimCluster
from fusion.orchestration.fanout import FanoutResult
from fusion.orchestration.ledger import RunLedger, TaskMetrics
from fusion.orchestration.refine import RefinementResult
from fusion.orchestration.schemas import PipelineEvals
from fusion.orchestration.strategy import MODE_SETTINGS, Mode, ModeSettings
from fusion.routing.policy import RoutingDecision
from fusion.telemetry.cost import CostComparison, UsageSummary
from fusion.telemetry.traces import OrchestrationTrace


@dataclass
class PanelResult:
    """Result from a single panel model."""

    model_name: str
    provider: str
    provider_model_id: str
    content: str
    evaluation: ModelResponseEval
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    cost_known: bool = True
    cost_is_estimate: bool = True
    latency_ms: float = 0.0


@dataclass
class PipelineResult:
    """Complete result from an orchestration pipeline."""

    run_id: str
    task_type: str
    context_eval: ContextEvalResult
    panel_results: list[PanelResult]
    final_answer: str
    structured_output: dict[str, Any]
    final_eval: FinalEvalResult
    disagreement: dict[str, Any]
    routing: RoutingDecision
    trace: OrchestrationTrace
    total_cost_usd: float
    total_latency_ms: float
    usage: UsageSummary | None = None
    cost_comparison: CostComparison | None = None
    fanout: FanoutResult | None = None
    refinement: RefinementResult | None = None
    shadow: ShadowComparison | None = None
    warnings: list[str] = field(default_factory=list)
    evals: PipelineEvals | None = None
    ledger: RunLedger | None = None
    mode: Mode = Mode.REAL
    claims: list[ClaimCluster] = field(default_factory=list)
    agreement: AgreementReport | None = None
    cascade: CascadeOutcome | None = None
    budget: BudgetReport | None = None
    cache_hit: bool = False  # served from the response cache: no model was called
    # Why the run stopped early: insufficient_context, quorum, budget or timeout.
    halt_reason: str | None = None
    # Built from the panel's answers alone because the soft time limit passed first.
    partial: bool = False

    @property
    def mode_settings(self) -> ModeSettings:
        return MODE_SETTINGS[self.mode]

    @property
    def task_metrics(self) -> TaskMetrics | None:
        return self.ledger.task_metrics(self.total_latency_ms) if self.ledger else None


def build_usage_summary(
    ledger: RunLedger,
    *,
    model_order: list[str],
    wall_latency_ms: float,
    fanout: FanoutResult | None,
) -> UsageSummary:
    """Token and latency totals for Fusion's own calls, straight from the ledger."""
    per_model = ledger.usage_models(model_order)
    succeeded = [u for u in per_model if u.success]
    known_input = all(u.input_tokens is not None for u in succeeded)
    known_output = all(u.output_tokens is not None for u in succeeded)
    total_input = sum(u.input_tokens or 0 for u in per_model) if known_input else None
    total_output = sum(u.output_tokens or 0 for u in per_model) if known_output else None
    total_tokens = (
        total_input + total_output if total_input is not None and total_output is not None else None
    )
    synthesis = ledger.by_stage().get("synthesis")
    return UsageSummary(
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        total_tokens=total_tokens,
        per_model=per_model,
        fusion_wall_latency_ms=round(wall_latency_ms),
        panel_wall_latency_ms=fanout.panel_wall_latency_ms if fanout else None,
        synthesis_latency_ms=round(synthesis.latency_ms) if synthesis else None,
        total_model_call_latency_ms=round(sum(r.latency_ms for r in ledger.ordered_records())),
        max_panel_latency_ms=fanout.max_model_latency_ms if fanout else None,
        successful_model_calls=len(succeeded),
        failed_model_calls=len(per_model) - len(succeeded),
    )
