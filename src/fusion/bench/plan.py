"""``fusion bench plan``: what a study will cost and how long it will take, before it runs.

Every (task, arm) is priced with the same arithmetic as the hard cost caps
(``fusion.orchestration.budget_guard.plan_calls`` over the catalog's prices, from the task's own
size), plus what the scorer will spend. The estimate is a range, not a promise: answers are
assumed to be ``ANSWER_OUTPUT_TOKENS`` long, a cascade is assumed to escalate in
``ESCALATION_SHARE`` of cases, and the range scales every token count by ``LOW_FACTOR`` to
``HIGH_FACTOR``. When the plan does not fit the budget it suggests the largest study that does,
shrinking in a fixed order: fewer repeats, then fewer tasks, then fewer arms.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fusion.bench.scoring import Scorer, get_scorer
from fusion.bench.spec import BenchConfig, BenchTask, select_tasks
from fusion.orchestration.budget_guard import plan_calls
from fusion.orchestration.cascade import cheapest_first
from fusion.orchestration.strategy import Strategy
from fusion.providers.simulated import TIER_SPEED
from fusion.routing.budget import PlannedCall, estimate_tokens, forecast_calls
from fusion.routing.model_registry import ModelRegistry
from fusion.routing.policy import RoutingPolicy
from fusion.telemetry.cost import PricingRegistry

__all__ = [
    "ESCALATION_SHARE",
    "HIGH_FACTOR",
    "LOW_FACTOR",
    "ArmEstimate",
    "BenchPlan",
    "JobEstimate",
    "PlanSuggestion",
    "estimate_job",
    "make_plan",
]

ESCALATION_SHARE = 0.4  # provisional: how often a cascade is assumed to ask its whole panel
LOW_FACTOR = 0.3  # the low end of the range: a third of the assumed tokens (terse models)
HIGH_FACTOR = 2.5  # the high end: two and a half times as many (reasoning models, long reviews)


@dataclass(frozen=True)
class JobEstimate:
    """One (task, arm) run: expected cost, the most it can cost, and how long it takes."""

    expected_usd: float
    worst_usd: float  # everything the strategy might call, a cascade escalating
    low_usd: float
    high_usd: float
    eval_usd: float  # the scorer's share, included in the totals above
    seconds: float
    unpriced: tuple[str, ...] = ()


@dataclass
class ArmEstimate:
    arm: str
    expected_usd: float = 0.0
    low_usd: float = 0.0
    high_usd: float = 0.0
    jobs: int = 0


@dataclass
class PlanSuggestion:
    """The largest study that fits the budget."""

    repeats: int
    limit: int | None  # None: every task
    arms: list[str]
    expected_usd: float
    note: str


@dataclass
class BenchPlan:
    tasks: int
    arms: list[str]
    repeats: int
    jobs: int
    expected_usd: float
    low_usd: float
    high_usd: float
    wall_seconds: float
    budget_usd: float  # the smaller of --max-usd and what the live-spend cap has left
    per_arm: list[ArmEstimate] = field(default_factory=list)
    unpriced: list[str] = field(default_factory=list)
    suggestion: PlanSuggestion | None = None
    live: bool = True  # False for simulated runs, which spend nothing

    @property
    def fits(self) -> bool:
        """Passing: the expected cost is within budget and every model has a price (live runs)."""
        if not self.live:
            return True
        return self.expected_usd <= self.budget_usd + 1e-9 and not self.unpriced


def _call_seconds(call: PlannedCall, registry: ModelRegistry) -> float:
    entry = registry.models.get(call.alias)
    ttft_ms, tokens_per_s = TIER_SPEED[entry.latency_tier if entry else "medium"]
    return ttft_ms / 1000 + call.output_tokens / tokens_per_s


def _seconds(calls: list[PlannedCall], registry: ModelRegistry) -> float:
    """Critical path: the panel, then each refinement round, then synthesis next to the judge."""
    by_stage: dict[str, float] = {}
    for call in calls:
        by_stage[call.stage] = max(by_stage.get(call.stage, 0.0), _call_seconds(call, registry))
    refine = by_stage.get("refine", 0.0)
    rounds = sum(1 for c in calls if c.stage == "refine") // max(
        sum(1 for c in calls if c.stage == "panel"), 1
    )
    tail = max(
        by_stage.get("judge", 0.0) + by_stage.get("eval", 0.0), by_stage.get("synthesis", 0.0)
    )
    return by_stage.get("panel", 0.0) + refine * rounds + tail


def _scaled(calls: list[PlannedCall], factor: float) -> list[PlannedCall]:
    """The calls with every token count (input and output) multiplied by ``factor``."""
    return [
        PlannedCall(
            c.stage,
            c.alias,
            max(int(c.input_tokens * factor), 1),
            max(int(c.output_tokens * factor), 1),
        )
        for c in calls
    ]


def estimate_job(
    task: BenchTask,
    strategy: Strategy,
    *,
    routing: RoutingPolicy,
    registry: ModelRegistry,
    pricing: PricingRegistry,
    scorer: Scorer,
) -> JobEstimate:
    """Forecast one run of ``strategy`` on ``task`` from the catalog."""
    decision = routing.router.route(
        strategy=strategy, explicit_type=task.task_type.value, content=task.prompt
    )
    members = (
        cheapest_first(strategy.members, registry.models, pricing)
        if strategy.kind == "cascade"
        else strategy.members
    )
    plan = strategy.model_copy(
        update={"members": members, "aggregator_model": decision.synthesizer_model or None}
    )
    size = "\n".join([task.prompt, task.context, *task.files.values()])
    prompt_tokens = estimate_tokens(size)
    first = plan_calls(
        plan,
        judge_model=decision.judge_model,
        prompt_tokens=prompt_tokens,
        include_escalation=False,
    )
    full = plan_calls(
        plan, judge_model=decision.judge_model, prompt_tokens=prompt_tokens, include_escalation=True
    )

    def price(calls: list[PlannedCall]) -> tuple[float, tuple[str, ...]]:
        found = forecast_calls(calls, registry.models, pricing)
        return found.usd, found.unpriced

    first_usd, _ = price(first)
    full_usd, unpriced = price(full)
    expected = first_usd + ESCALATION_SHARE * (full_usd - first_usd)
    low = price(_scaled(first, LOW_FACTOR))[0]
    high = price(_scaled(full, HIGH_FACTOR))[0]
    eval_usd = scorer.estimate_usd(task)
    seconds = _seconds(first, registry) + ESCALATION_SHARE * (
        _seconds(full, registry) - _seconds(first, registry)
    )
    return JobEstimate(
        expected_usd=expected + eval_usd,
        worst_usd=full_usd + eval_usd,
        low_usd=low + eval_usd,
        high_usd=high + eval_usd,
        eval_usd=eval_usd,
        seconds=seconds,
        unpriced=unpriced,
    )


def make_plan(
    cfg: BenchConfig,
    tasks: list[BenchTask],
    strategies: dict[str, Strategy],
    *,
    routing: RoutingPolicy,
    registry: ModelRegistry,
    pricing: PricingRegistry,
    spend_left_usd: float | None = None,
) -> BenchPlan:
    """The cost and time of ``cfg`` over ``tasks``, with a suggestion when it does not fit.

    ``strategies`` maps each arm name to its resolved strategy; ``spend_left_usd`` is what the
    live-spend cap has left (None for simulated runs).
    """
    grid: dict[str, dict[str, JobEstimate]] = {}
    for arm in cfg.arms:
        grid[arm.name] = {
            t.id: estimate_job(
                t,
                strategies[arm.name],
                routing=routing,
                registry=registry,
                pricing=pricing,
                scorer=get_scorer(t.category),
            )
            for t in tasks
        }
    live = not cfg.mock
    budget = cfg.max_usd if spend_left_usd is None else min(cfg.max_usd, spend_left_usd)
    per_arm = [
        ArmEstimate(
            arm=name,
            expected_usd=cfg.repeats * sum(e.expected_usd for e in jobs.values()),
            low_usd=cfg.repeats * sum(e.low_usd for e in jobs.values()),
            high_usd=cfg.repeats * sum(e.high_usd for e in jobs.values()),
            jobs=cfg.repeats * len(jobs),
        )
        for name, jobs in grid.items()
    ]
    seconds = cfg.repeats * sum(e.seconds for jobs in grid.values() for e in jobs.values())
    unpriced = sorted({a for jobs in grid.values() for e in jobs.values() for a in e.unpriced})
    plan = BenchPlan(
        tasks=len(tasks),
        arms=[a.name for a in cfg.arms],
        repeats=cfg.repeats,
        jobs=sum(a.jobs for a in per_arm),
        expected_usd=sum(a.expected_usd for a in per_arm),
        low_usd=sum(a.low_usd for a in per_arm),
        high_usd=sum(a.high_usd for a in per_arm),
        wall_seconds=seconds / cfg.concurrency,
        budget_usd=budget,
        per_arm=per_arm,
        unpriced=unpriced,
        live=live,
    )
    if live and plan.expected_usd > budget + 1e-9:
        plan.suggestion = _suggest(cfg, tasks, grid, budget)
    return plan


def _suggest(
    cfg: BenchConfig,
    tasks: list[BenchTask],
    grid: dict[str, dict[str, JobEstimate]],
    budget: float,
) -> PlanSuggestion | None:
    """Shrink in order: repeats, then tasks, then arms. The first design that fits wins."""

    def cost(arms: list[str], chosen: list[BenchTask], repeats: int) -> float:
        return repeats * sum(grid[a][t.id].expected_usd for a in arms for t in chosen)

    arms = [a.name for a in cfg.arms]
    for repeats in range(cfg.repeats - 1, 0, -1):
        total = cost(arms, tasks, repeats)
        if total <= budget:
            return PlanSuggestion(repeats, None, arms, total, "fewer repeats")
    minimum = min(10, len(tasks))
    remaining = list(arms)
    while remaining:
        for n in range(len(tasks), 0, -1):
            chosen = select_tasks(tasks, n, cfg.seed)
            total = cost(remaining, chosen, 1)
            if total <= budget:
                if n >= minimum or len(remaining) == 1:
                    dropped = [a for a in arms if a not in remaining]
                    note = "fewer repeats and tasks" + (
                        f", without {', '.join(dropped)}" if dropped else ""
                    )
                    return PlanSuggestion(1, None if n == len(tasks) else n, remaining, total, note)
                break
        dearest = max(remaining, key=lambda a: sum(grid[a][t.id].expected_usd for t in tasks))
        remaining.remove(dearest)
    return None
