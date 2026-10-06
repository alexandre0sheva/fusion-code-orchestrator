"""The pipeline engine: runs the stages in order over one ``RunState``."""

from __future__ import annotations

import asyncio
from typing import Any

from fusion.config.loader import BaselineEntry
from fusion.evals.engine import EvalEngine
from fusion.orchestration.cache import ResponseCache, request_key
from fusion.orchestration.context import PipelineContext, PipelineDeps, RunState
from fusion.orchestration.ledger import RunLedger
from fusion.orchestration.output import ResultPresenter
from fusion.orchestration.progress import report as report_progress
from fusion.orchestration.result import PipelineResult
from fusion.orchestration.schemas import CostLatencyInfo, Detail
from fusion.orchestration.stages import SoftTimeoutRecovery, Stage, default_stages
from fusion.orchestration.strategy import Mode
from fusion.providers.base import ModelProvider
from fusion.routing.classifier import TaskType
from fusion.routing.model_registry import ModelRegistry
from fusion.routing.policy import RoutingPolicy
from fusion.security.policy import SecurityPolicy
from fusion.storage.run_store import RunStore
from fusion.telemetry.cost import PricingRegistry


async def _stop_background(state: RunState) -> None:
    """Cancel work a run started but did not collect (a halted or failed run's shadow call)."""
    task = state.shadow_task
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class BasePipeline:
    """Shared multi-model orchestration engine, composed from stages."""

    task_type: TaskType = TaskType.DEFAULT
    # Seconds after which a real-mode run stops waiting for models and returns what the panel has
    # produced (the MCP server sets it from FUSION_TOOL_SOFT_TIMEOUT_S); None means no limit.
    soft_timeout_s: float | None = None

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
        self._cache = ResponseCache(routing.budgets.cache)

    # Read-only views kept for callers that poke at a pipeline's collaborators.
    @property
    def _routing(self) -> RoutingPolicy:
        return self.deps.routing

    @property
    def _run_store(self) -> RunStore:
        return self.deps.run_store

    async def run(
        self,
        ctx: PipelineContext,
        *,
        mode: Mode = Mode.REAL,
        seed: int | None = None,
        redact: bool | None = None,
        ledger: RunLedger | None = None,
    ) -> PipelineResult:
        """Execute every stage; halted runs skip to the stages that must always run.

        The strategy comes from ``ctx.strategy`` (or ``ctx.budget``); ``mode`` says whether the run
        serves Claude Code (real) or is measured in a study (benchmark). A study varies ``seed``
        per repeat, may turn ``redact`` back on, and passes its own ``ledger`` so the money a run
        spent is known even when a stage raises.
        """
        state = RunState.start(ctx, self.deps, mode, seed=seed, redact=redact, ledger=ledger)
        key = self._cache_key(state) if mode is Mode.REAL else None
        if key is not None and (hit := self._cache.get(key)) is not None:
            return hit
        soft = self.soft_timeout_s if mode is Mode.REAL else None
        deadline = asyncio.get_running_loop().time() + soft if soft else None
        state.soft_limit_s = soft or 0.0
        try:
            for stage in self._stages:
                if state.halted and not stage.always_runs:
                    continue
                if state.partial and not stage.always_runs:
                    continue  # the answer is final; the shadow comparison would only delay it
                if label := getattr(stage, "label", None):
                    await report_progress(label)
                state = await self._run_stage(stage, state, deadline)
        finally:
            await _stop_background(state)
        assert state.result is not None, "the final stage must produce a result"
        if key is not None and not state.halted and not state.partial:
            self._cache.put(key, state.result)
        return state.result

    async def _run_stage(self, stage: Stage, state: RunState, deadline: float | None) -> RunState:
        """Run one stage; a stage under the soft time limit that overruns it is cut short and the
        run is finished from what it has (``SoftTimeoutRecovery``)."""
        if deadline is None or not getattr(stage, "soft_limited", False):
            return await stage.run(state)
        limit = asyncio.timeout_at(deadline)
        try:
            async with limit:
                return await stage.run(state)
        except TimeoutError:
            if not limit.expired():
                raise
        await report_progress("soft time limit reached; returning what the panel has")
        return await SoftTimeoutRecovery(self.deps).run(state)

    def _cache_key(self, state: RunState) -> str | None:
        """The response-cache key of this run, or None when the cache is off."""
        if not self._cache.enabled:
            return None
        assert state.strategy is not None
        return request_key(state.ctx, state.strategy)

    # -- output helpers used by the task-specific subclasses ----------------------------------

    def _common_output_fields(
        self, result: PipelineResult, title: str, detail: Detail = "compact"
    ) -> dict[str, Any]:
        return self.deps.presenter.common_output_fields(result, title, detail)

    def _build_cost_latency(self, result: PipelineResult) -> CostLatencyInfo:
        return self.deps.presenter.cost_latency(result)
