"""Run a study: every (task, arm, repeat) is a job, jobs run concurrently, and a run can resume.

A job is keyed by a hash of the task, the arm (its strategy and the model ids behind it), the
repeat and the seed, so a finished job is never run twice. Results are appended to the run's
``results.jsonl`` as each job ends; ``resume`` skips the keys already there, and a job that ended
in an error is tried again. Spending is held to two caps. ``max_usd`` limits the run and the
live-spend ledger limits everything ever run live (``spend``): before a job starts, its worst-case
cost is reserved against what is left, and the run stops cleanly, resumable, when it would not fit.
Because reservations are estimates, a run can pass a cap by at most the excess of the jobs in
flight.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from fusion.bench.arms import arm_book
from fusion.bench.cache import CachingProvider, ResponseDiskCache
from fusion.bench.metrics import build_metrics
from fusion.bench.plan import estimate_job
from fusion.bench.scoring import AnswerView, ScoreEnv, ScoreResult, get_scorer, is_solved
from fusion.bench.spec import (
    BenchConfig,
    BenchTask,
    load_dataset,
    select_tasks,
    task_hash,
)
from fusion.bench.spend import SpendLedger, default_ledger
from fusion.bench.store import (
    BenchItem,
    BenchStore,
    InlineRunStore,
    ItemStatus,
    RunStatus,
    new_bench_run_id,
)
from fusion.config.layers import ConfigError
from fusion.config.loader import RoutingPoliciesConfig, load_routing_policies
from fusion.config.paths import bench_results_dir
from fusion.evals.engine import EvalEngine
from fusion.orchestration.context import PipelineContext
from fusion.orchestration.factory import build_provider_registry
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.orchestration.pipeline import BasePipeline
from fusion.orchestration.strategy import Mode, Strategy, StrategyBook, load_strategy_book
from fusion.providers.base import ModelProvider
from fusion.providers.limits import ProviderLimiter
from fusion.providers.simulated import SimulatedProvider, SimWorld, sim_models_from_catalog
from fusion.routing.model_registry import ModelRegistry
from fusion.routing.policy import RoutingPolicy
from fusion.storage.run_store import RunStore
from fusion.telemetry.cost import PricingRegistry

__all__ = ["BenchEnv", "BenchProgress", "BenchRun", "Job", "build_env", "job_key", "run_bench"]

SPEND_TASK = "bench"  # the ``task`` field of spend-ledger entries written by studies
_EPSILON = 1e-9


@dataclass
class BenchEnv:
    """Everything a run needs from outside: providers, storage, prices and the spend ledger."""

    providers: dict[str, ModelProvider]
    store: BenchStore
    run_store: RunStore
    registry: ModelRegistry
    pricing: PricingRegistry
    book: StrategyBook
    routing_config: RoutingPoliciesConfig
    spend: SpendLedger | None = None  # live runs only; None records nothing
    cache: ResponseDiskCache | None = None
    sim_providers: list[SimulatedProvider] = field(default_factory=list)


def build_env(
    cfg: BenchConfig,
    tasks: list[BenchTask],
    *,
    results_dir: Path | None = None,
    providers: bool = True,
) -> BenchEnv:
    """The environment for ``cfg``: simulated providers for ``--mock``, real ones otherwise.

    ``providers=False`` builds none (planning only needs the catalog, not API keys).
    """
    root = results_dir or bench_results_dir()
    registry = ModelRegistry.for_mode(use_mock=False)
    pricing = PricingRegistry()
    store = BenchStore(root)
    common = {
        "registry": registry,
        "pricing": pricing,
        "book": load_strategy_book(),
        "routing_config": load_routing_policies(),
        "store": store,
    }
    if cfg.mock:
        return _mock_env(cfg, tasks, root, **common)  # type: ignore[arg-type]
    live = build_provider_registry(use_mock=False) if providers else {}
    cache = ResponseDiskCache(root / "cache") if cfg.cache else None
    if cache is not None:
        live = {n: CachingProvider(p, cache) for n, p in live.items()}
    return BenchEnv(
        providers=live,
        run_store=RunStore(db_path=str(root / "bench.db")),
        spend=default_ledger() if results_dir is None else SpendLedger(root / "spend.json"),
        cache=cache,
        **common,  # type: ignore[arg-type]
    )


def _mock_env(
    cfg: BenchConfig,
    tasks: list[BenchTask],
    root: Path,
    *,
    registry: ModelRegistry,
    pricing: PricingRegistry,
    book: StrategyBook,
    routing_config: RoutingPoliciesConfig,
    store: BenchStore,
) -> BenchEnv:
    """Simulated twins of every catalog provider: same models and prices, no network, no spend."""
    world = SimWorld(tasks, seed=cfg.seed)
    catalog = pricing.catalog
    limits = catalog.provider_limits
    providers: dict[str, ModelProvider] = {}
    sims: list[SimulatedProvider] = []
    for name in sorted({e.provider for e in registry.models.values()}):
        limit = limits.get(name)
        limiter = (
            ProviderLimiter(
                limit.max_concurrent, limit.rpm, clock=lambda: asyncio.get_running_loop().time()
            )
            if limit
            else None
        )
        sim = SimulatedProvider(
            name, sim_models_from_catalog(catalog, name), world, limiter=limiter
        )
        sims.append(sim)
        providers[name] = sim
    return BenchEnv(
        providers=providers,
        store=store,
        run_store=InlineRunStore(db_path=str(root / "bench.db")),
        registry=registry,
        pricing=pricing,
        book=book,
        routing_config=routing_config,
        spend=None,
        cache=None,
        sim_providers=sims,
    )


# ------------------------------------------------------------------------------------------ jobs


@dataclass(frozen=True)
class Job:
    key: str
    task: BenchTask
    arm: str
    repeat: int
    seed: int
    worst_usd: float  # what the job may cost, for reserving against the caps


def job_key(
    task: BenchTask,
    strategy: Strategy,
    registry: ModelRegistry,
    repeat: int,
    seed: int,
    redact: bool,
) -> str:
    """Hash of everything that decides a job's result; the same job has the same key forever."""
    models = {
        alias: registry.models[alias].model_id
        for alias in strategy.models
        if alias in registry.models
    }
    blob = json.dumps(
        {
            "task": task_hash(task),
            "strategy": strategy.model_dump(mode="json"),
            "models": models,
            "repeat": repeat,
            "seed": seed,
            "redact": redact,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


@dataclass(frozen=True)
class BenchProgress:
    done: int  # jobs finished, including those a resumed run found already done
    total: int
    spent_usd: float


class BenchRun(BaseModel):
    """The outcome of ``run_bench``."""

    run_id: str
    status: RunStatus
    stop_reason: str | None = None
    config: BenchConfig
    total_jobs: int
    done_jobs: int  # finished (completed or halted), including those found on resume
    resumed_jobs: int = 0  # of those, how many were already done when this call began
    failed_jobs: int = 0  # ended in an error; ``resume`` tries them again
    spent_usd: float
    eval_spent_usd: float
    cache_hits: int = 0
    items: list[BenchItem]


class _Governor:
    """Reserves each job's worst-case cost against both caps before it starts."""

    def __init__(self, max_usd: float, spent: float, spend: SpendLedger | None) -> None:
        self.max_usd = max_usd
        self.spent = spent
        self.reserved = 0.0
        self.spend = spend
        self.reason: str | None = None
        self._changed = asyncio.Condition()

    def _left(self) -> tuple[float, str]:
        run_left = self.max_usd - self.spent
        if self.spend is None:
            return run_left, "run"
        global_left = self.spend.remaining()
        return (run_left, "run") if run_left <= global_left else (global_left, "live-spend cap")

    async def acquire(self, estimate: float) -> bool:
        """Wait until ``estimate`` fits, or stop the run (False) when it never will."""
        async with self._changed:
            while True:
                if self.reason is not None:
                    return False
                left, which = self._left()
                if estimate > left + _EPSILON:
                    self.reason = self._explain(estimate, left, which)
                    self._changed.notify_all()
                    return False
                if self.reserved == 0 or self.reserved + estimate <= left + _EPSILON:
                    self.reserved += estimate
                    return True
                await self._changed.wait()  # jobs in flight may settle for less than reserved

    def _explain(self, estimate: float, left: float, which: str) -> str:
        if which == "run":
            return (
                f"max_usd reached: ${self.spent:.4f} of ${self.max_usd:.2f} spent and the next "
                f"job may cost up to ${estimate:.4f}"
            )
        assert self.spend is not None
        return (
            f"the ${self.spend.cap_usd:.2f} live-spend cap would be passed: "
            f"${self.spend.total():.4f} spent overall and the next job may cost up to "
            f"${estimate:.4f}"
        )

    async def settle(self, estimate: float, actual: float) -> None:
        async with self._changed:
            self.reserved -= estimate
            self.spent += actual
            self._changed.notify_all()


# --------------------------------------------------------------------------------------- the run


def _pipeline(env: BenchEnv, routing: RoutingPolicy, clock: Callable[[], float]) -> BasePipeline:
    engine = EvalEngine(registry=env.registry, provider_resolver=env.providers, use_llm_judge=True)
    pipeline = BasePipeline(
        registry=env.registry,
        routing=routing,
        providers=env.providers,
        eval_engine=engine,
        run_store=env.run_store,
        pricing=env.pricing,
        shadow_baseline=None,
    )
    pipeline.deps.clock = clock
    return pipeline


def _context(task: BenchTask, arm: str) -> PipelineContext:
    return PipelineContext(
        task_type=task.task_type,
        primary_content=task.prompt,
        context=task.context,
        file_snippets=[f"{path}\n{content}" for path, content in task.files.items()],
        changed_files=list(task.files),
        strategy=arm,
    )


async def run_bench(
    cfg: BenchConfig,
    *,
    env: BenchEnv | None = None,
    run_id: str | None = None,
    on_item: Callable[[BenchItem, BenchProgress], None] | None = None,
) -> BenchRun:
    """Run (or resume, when ``run_id`` names an existing run) the study ``cfg`` describes."""
    tasks = select_tasks(load_dataset(cfg.dataset), cfg.limit, cfg.seed)
    env = env or build_env(cfg, tasks)
    unknown = [m for m in cfg.judge_models if m not in env.registry.models]
    if unknown:
        msg = f"judge_models names models that are not in the catalog: {', '.join(unknown)}"
        raise ConfigError(msg)
    clock = asyncio.get_running_loop().time
    book = arm_book(env.book, cfg.arms)
    routing = RoutingPolicy(env.routing_config, registry=env.registry, strategies=book)
    pipeline = _pipeline(env, routing, clock)
    store = env.store
    spend = env.spend if not cfg.mock else None
    if spend is not None:
        spend.check(0.0, "starting a live run")

    strategies = {arm.name: book.get(arm.name) for arm in cfg.arms}
    jobs: list[Job] = []
    # Repeats outermost, so a run that stops early has whole repeats rather than whole arms.
    for repeat in range(1, cfg.repeats + 1):
        seed = cfg.seed + repeat - 1
        for task in tasks:
            for arm in cfg.arms:
                strategy = strategies[arm.name]
                jobs.append(
                    Job(
                        key=job_key(task, strategy, env.registry, repeat, seed, cfg.redact),
                        task=task,
                        arm=arm.name,
                        repeat=repeat,
                        seed=seed,
                        worst_usd=_worst_case(task, strategy, routing, env),
                    )
                )

    rid = run_id or new_bench_run_id()
    existing = store.get_run(rid)
    if existing is None:
        store.create_run(rid, cfg, len(jobs))
    else:
        store.sync_index(rid)
        store.update_run(rid, status="running", total_jobs=len(jobs), config=cfg)
    finished = store.completed_keys(rid)
    wanted = {j.key for j in jobs}
    resumed = len(finished & wanted)
    pending = [j for j in jobs if j.key not in finished]
    spent, eval_spent = store.spent(rid)
    governor = _Governor(cfg.max_usd, spent + eval_spent, spend)
    gate = asyncio.Semaphore(cfg.concurrency)
    done = resumed

    async def work(job: Job) -> None:
        nonlocal done, spent, eval_spent
        async with gate:
            if not await governor.acquire(job.worst_usd):
                return
            item: BenchItem | None = None
            try:
                item = await _execute(job, rid, cfg, pipeline, env, clock)
            finally:
                actual = item.metrics.cost_usd + item.metrics.eval_cost_usd if item else 0.0
                if spend is not None and actual > 0:
                    spend.append(actual, task=SPEND_TASK, purpose=f"{rid} {job.arm} {job.task.id}")
                await governor.settle(job.worst_usd, actual)
            store.add_item(item)
            spent += item.metrics.cost_usd
            eval_spent += item.metrics.eval_cost_usd
            if item.status in ("completed", "halted"):
                done += 1
            store.update_run(
                rid,
                status="running",
                done_jobs=done,
                spent_usd=spent,
                eval_spent_usd=eval_spent,
            )
            if on_item is not None:
                on_item(item, BenchProgress(done, len(jobs), spent + eval_spent))

    status: RunStatus = "failed"
    workers = [asyncio.ensure_future(work(j)) for j in pending]
    try:
        await asyncio.gather(*workers)
        status = "stopped" if governor.reason else "completed"
    except BaseException as exc:
        for worker in workers:  # gather leaves the others running when one fails
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if isinstance(exc, asyncio.CancelledError):
            status = "interrupted"
        raise
    finally:
        store.update_run(
            rid,
            status=status,
            stop_reason=governor.reason,
            done_jobs=done,
            spent_usd=spent,
            eval_spent_usd=eval_spent,
        )
    items = [i for i in store.items(rid) if i.job_key in wanted]
    return BenchRun(
        run_id=rid,
        status=status,
        stop_reason=governor.reason,
        config=cfg,
        total_jobs=len(jobs),
        done_jobs=done,
        resumed_jobs=resumed,
        failed_jobs=sum(1 for i in items if i.status == "error"),
        spent_usd=spent,
        eval_spent_usd=eval_spent,
        cache_hits=sum(i.metrics.cache_hits for i in items),
        items=items,
    )


def _worst_case(
    task: BenchTask, strategy: Strategy, routing: RoutingPolicy, env: BenchEnv
) -> float:
    return estimate_job(
        task,
        strategy,
        routing=routing,
        registry=env.registry,
        pricing=env.pricing,
        scorer=get_scorer(task.category),
    ).worst_usd


async def _execute(
    job: Job,
    run_id: str,
    cfg: BenchConfig,
    pipeline: BasePipeline,
    env: BenchEnv,
    clock: Callable[[], float],
) -> BenchItem:
    """Run one job and score it. Never raises for a failed run: that becomes an ``error`` item."""
    task = job.task
    ledger = RunLedger(clock)
    began = clock()
    try:
        result = await pipeline.run(
            _context(task, job.arm),
            mode=Mode.BENCHMARK,
            seed=job.seed,
            redact=cfg.redact,
            ledger=ledger,
        )
    except Exception as exc:  # noqa: BLE001 — a failed run is a result, not a crash of the study
        wall_ms = (clock() - began) * 1000
        return BenchItem(
            run_id=run_id,
            job_key=job.key,
            task_id=task.id,
            category=task.category,
            arm=job.arm,
            repeat=job.repeat,
            seed=job.seed,
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            calls=list(ledger.records),
            metrics=build_metrics(ledger, wall_ms=wall_ms),
        )
    view = AnswerView(
        final_answer=result.final_answer,
        claims=[c.model_dump(mode="json") for c in result.claims],
        structured=result.structured_output,
        halted=result.halt_reason is not None,
    )
    scoring = RunLedger(clock)
    gateway = CallGateway(
        ledger=scoring,
        models=env.registry.models,
        providers=env.providers,
        pricing=env.pricing,
        truncate_prompts=False,
        temperature=0.0,
        seed=job.seed,
    )
    score: ScoreResult | None = None
    error: str | None = None
    try:
        score = await get_scorer(task.category).score(
            task, view, ScoreEnv(gateway=gateway, judge_models=cfg.judge_models)
        )
    except Exception as exc:  # noqa: BLE001 — the answer is kept; a resume scores it again
        error = f"scoring failed: {type(exc).__name__}: {exc}"
    quality = score.quality if score else None
    metrics = build_metrics(
        ledger,
        wall_ms=result.total_latency_ms,
        eval_cost_usd=scoring.total_cost().usd,
        quality=quality,
        solved=is_solved(task.category, quality),
    )
    status: ItemStatus = "error" if error else ("halted" if result.halt_reason else "completed")
    return BenchItem(
        run_id=run_id,
        job_key=job.key,
        task_id=task.id,
        category=task.category,
        arm=job.arm,
        repeat=job.repeat,
        seed=job.seed,
        status=status,
        error=error,
        answer=result.final_answer,
        claims=view.claims,
        agreement=result.agreement.score if result.agreement else None,
        warnings=list(result.warnings),
        calls=list(ledger.records),
        metrics=metrics,
        score=score,
    )
