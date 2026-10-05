"""Pipeline input context, shared dependencies and the mutable state stages pass along."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from fusion.benchmark.shadow import BaselineCall, ShadowComparison
from fusion.config.loader import BaselineEntry
from fusion.evals.engine import EvalEngine
from fusion.evals.schemas import (
    ContextEvalResult,
    FinalEvalResult,
    HybridEvalResult,
    ModelResponseEval,
)
from fusion.orchestration.claims import AgreementReport, ClaimCluster, PanelAnswer
from fusion.orchestration.fanout import FanoutResult
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.orchestration.refine import RefinementResult
from fusion.orchestration.schemas import PipelineEvals
from fusion.orchestration.strategy import MODE_SETTINGS, Mode, ModeSettings, PanelMember, Strategy
from fusion.providers.base import ModelProvider, ModelResponse
from fusion.routing.budget import BudgetLevel, BudgetTracker
from fusion.routing.classifier import TaskType
from fusion.routing.model_registry import ModelRegistry
from fusion.routing.policy import RoutingDecision, RoutingPolicy
from fusion.security.policy import SecurityPolicy
from fusion.storage.run_store import RunStore
from fusion.telemetry.cost import PricingRegistry
from fusion.telemetry.traces import OrchestrationTrace

if TYPE_CHECKING:
    from fusion.orchestration.output import ResultPresenter
    from fusion.orchestration.result import PanelResult, PipelineResult


@dataclass
class PipelineContext:
    """Input context for a pipeline run."""

    task_type: TaskType
    primary_content: str
    context: str = ""
    file_snippets: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    budget: BudgetLevel = BudgetLevel.MEDIUM
    strategy: str | None = None  # a strategy name wins over ``budget``
    max_models: int | None = None
    shadow_baseline: bool | None = None


@dataclass
class PipelineDeps:
    """Everything stages need; built once by the factory and shared by every run."""

    registry: ModelRegistry
    routing: RoutingPolicy
    providers: dict[str, ModelProvider]
    eval_engine: EvalEngine
    run_store: RunStore
    pricing: PricingRegistry
    security: SecurityPolicy
    presenter: ResultPresenter
    # Fixed baseline for shadow comparisons (offline mode); None means "from baseline config".
    shadow_baseline: BaselineEntry | None = None
    clock: Callable[[], float] = time.perf_counter


@dataclass
class Halt:
    """Why a run stopped early; the persist stage still produces a diagnostic result."""

    reason: Literal["insufficient_context", "quorum"]


@dataclass
class RunState:
    """Everything one run has produced so far. Stages read and write only this."""

    ctx: PipelineContext
    mode: Mode
    started: float
    ledger: RunLedger
    gateway: CallGateway
    budget: BudgetTracker
    warnings: list[str] = field(default_factory=list)

    run_id: str = ""
    trace: OrchestrationTrace | None = None
    sanitized_primary: str = ""
    sanitized_context: str = ""
    sanitized_snippets: list[str] = field(default_factory=list)
    redaction_count: int = 0

    strategy: Strategy | None = None
    routing: RoutingDecision | None = None
    members: list[PanelMember] = field(default_factory=list)
    panel_models: list[str] = field(default_factory=list)
    judge_model: str = ""
    synthesizer_model: str = ""
    context_eval: ContextEvalResult | None = None

    fanout: FanoutResult | None = None
    successful: list[tuple[str, ModelResponse]] = field(default_factory=list)
    refinement: RefinementResult | None = None
    answers: dict[str, PanelAnswer] = field(default_factory=dict)
    answer_structured: dict[str, bool] = field(default_factory=dict)
    clusters: list[ClaimCluster] = field(default_factory=list)
    agreement: AgreementReport | None = None
    evaluations: list[ModelResponseEval] = field(default_factory=list)
    judge_quality: HybridEvalResult | None = None
    panel_results: list[PanelResult] = field(default_factory=list)

    disagreement: dict[str, Any] = field(default_factory=dict)
    synth_response: ModelResponse | None = None
    final_answer: str = ""
    structured: dict[str, Any] = field(default_factory=dict)
    final_eval: FinalEvalResult | None = None
    evals: PipelineEvals | None = None
    shadow: ShadowComparison | None = None
    shadow_task: asyncio.Task[BaselineCall] | None = None

    halt: Halt | None = None
    total_latency_ms: float = 0.0
    result: PipelineResult | None = None

    @property
    def task_type(self) -> TaskType:
        return self.ctx.task_type

    @property
    def mode_settings(self) -> ModeSettings:
        return MODE_SETTINGS[self.mode]

    def stamp_latency(self, clock: Callable[[], float]) -> None:
        """Record the run's wall time so far (shadow work is measured separately)."""
        self.total_latency_ms = (clock() - self.started) * 1000

    @property
    def halted(self) -> bool:
        return self.halt is not None

    @classmethod
    def start(
        cls, ctx: PipelineContext, deps: PipelineDeps, mode: Mode = Mode.REAL
    ) -> RunState:
        warnings: list[str] = []
        ledger = RunLedger(deps.clock)
        settings = MODE_SETTINGS[mode]
        gateway = CallGateway(
            ledger=ledger,
            models=deps.registry.models,
            providers=deps.providers,
            pricing=deps.pricing,
            warnings=warnings,
            truncate_prompts=settings.truncate_prompts,
            temperature=settings.temperature,
            seed=settings.seed,
        )
        return cls(
            ctx=ctx,
            mode=mode,
            strategy=deps.routing.resolve_strategy(ctx.strategy, ctx.budget),
            started=deps.clock(),
            ledger=ledger,
            gateway=gateway,
            budget=BudgetTracker(config=deps.routing.budgets.budgets),
            warnings=warnings,
        )
