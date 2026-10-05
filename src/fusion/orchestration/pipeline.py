"""The pipeline engine: runs the stages in order over one ``RunState``."""

from __future__ import annotations

from typing import Any

from fusion.config.loader import BaselineEntry
from fusion.evals.engine import EvalEngine
from fusion.orchestration.context import PipelineContext, PipelineDeps, RunState
from fusion.orchestration.output import ResultPresenter
from fusion.orchestration.result import PipelineResult
from fusion.orchestration.schemas import CostLatencyInfo
from fusion.orchestration.stages import Stage, default_stages
from fusion.providers.base import ModelProvider
from fusion.routing.classifier import TaskType
from fusion.routing.model_registry import ModelRegistry
from fusion.routing.policy import RoutingPolicy
from fusion.security.policy import SecurityPolicy
from fusion.storage.run_store import RunStore
from fusion.telemetry.cost import PricingRegistry


class BasePipeline:
    """Shared multi-model orchestration engine, composed from stages."""

    task_type: TaskType = TaskType.DEFAULT

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        routing: RoutingPolicy,
        providers: dict[str, ModelProvider],
        eval_engine: EvalEngine,
        run_store: RunStore,
        security_policy: SecurityPolicy | None = None,
        pricing: PricingRegistry | None = None,
        shadow_baseline: BaselineEntry | None = None,
        stages: list[Stage] | None = None,
    ) -> None:
        pricing = pricing or PricingRegistry()
        self.deps = PipelineDeps(
            registry=registry,
            routing=routing,
            providers=providers,
            eval_engine=eval_engine,
            run_store=run_store,
            pricing=pricing,
            security=security_policy or SecurityPolicy.from_env(),
            presenter=ResultPresenter(
                eval_engine=eval_engine, run_store=run_store, pricing=pricing
            ),
            shadow_baseline=shadow_baseline,
        )
        self._stages = stages if stages is not None else default_stages(self.deps)

    # Read-only views kept for callers that poke at a pipeline's collaborators.
    @property
    def _routing(self) -> RoutingPolicy:
        return self.deps.routing

    @property
    def _run_store(self) -> RunStore:
        return self.deps.run_store

    async def run(self, ctx: PipelineContext) -> PipelineResult:
        """Execute every stage; halted runs skip to the stages that must always run."""
        state = RunState.start(ctx, self.deps)
        for stage in self._stages:
            if state.halted and not stage.always_runs:
                continue
            state = await stage.run(state)
        assert state.result is not None, "the final stage must produce a result"
        return state.result

    # -- output helpers used by the task-specific subclasses ----------------------------------

    def _common_output_fields(self, result: PipelineResult, title: str) -> dict[str, Any]:
        return self.deps.presenter.common_output_fields(result, title)

    def _build_cost_latency(self, result: PipelineResult) -> CostLatencyInfo:
        return self.deps.presenter.cost_latency(result)
