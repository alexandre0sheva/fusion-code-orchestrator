"""The statistics of a study: what each arm achieved, how arms compare, and what that supports.

Every interval resamples **tasks**, never single runs: the repeats of a task are not independent
(same prompt, same truth), so resampling them would make the intervals too narrow. A comparison of
two arms is **paired**, on the tasks both of them answered. Pure Python: a study has at most a few
hundred tasks, so a 2000-resample bootstrap costs well under a second.

``verdicts`` turns a comparison into the four claims a study exists to settle (cheaper, faster,
not worse, better), each *yes*, *no* or *inconclusive*, with the reason in words. Inconclusive is a
result: it says the study was too small or too noisy to tell.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import fmean, stdev
from typing import Literal

from pydantic import BaseModel, Field

from fusion.bench.costing import full_cost_usd
from fusion.bench.store import BenchItem

__all__ = [
    "DEFAULT_BOOTSTRAP",
    "DEFAULT_MARGIN",
    "DEFAULT_MIN_TASKS",
    "ArmStats",
    "Cell",
    "Comparison",
    "Interval",
    "PairedQuality",
    "Rules",
    "StudyStats",
    "Verdict",
    "bootstrap_interval",
    "build_cells",
    "compare_arms",
    "pareto_frontier",
    "percentile",
    "sign_test_p",
    "study_stats",
    "verdicts",
]

DEFAULT_BOOTSTRAP = 2000  # resamples per interval
DEFAULT_MARGIN = 0.03  # quality an arm may lose and still count as "not worse"
DEFAULT_MIN_TASKS = 10  # paired tasks below which no claim is made
DEFAULT_TIE = 0.02  # a task whose two arms differ by no more than this is a tie
_VALID_SHARE = 0.9  # resamples that must be computable (a ratio needs a solved task in each)

Claim = Literal["cheaper", "faster", "not_worse", "better"]
Outcome = Literal["yes", "no", "inconclusive", "blocked"]
CLAIMS: tuple[Claim, ...] = ("cheaper", "faster", "not_worse", "better")


# ------------------------------------------------------------------------------- small helpers


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0..1), or None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(q * len(ordered)) - 1, 0)]


def sign_test_p(wins: int, losses: int) -> float | None:
    """Two-sided exact sign test: how likely ``wins`` versus ``losses`` is if the arms were equal.
    Ties are not counted. None when nothing differed."""
    n = wins + losses
    if n == 0:
        return None
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / float(2**n)
    return min(1.0, 2.0 * tail)


def _mean(values: Sequence[float]) -> float | None:
    return float(fmean(values)) if values else None


# ---------------------------------------------------------------------------------- the cells


@dataclass(frozen=True)
class Cell:
    """One arm's results on one task, the repeats pooled: the unit that is resampled."""

    task_id: str
    category: str
    difficulty: str
    n: int  # items (repeats) written
    scored: int  # of those, with a quality
    solved: int
    quality: float | None  # mean over the scored repeats
    quality_sd: float | None  # spread across repeats (needs two scored)
    cost: float  # total over all repeats: money was spent whether or not it scored
    seconds: float | None  # mean wall seconds over repeats that measured the arm's own speed
    tokens: int  # input + output tokens, all repeats


def build_cells(
    items: Sequence[BenchItem], difficulties: dict[str, str] | None = None
) -> dict[str, dict[str, Cell]]:
    """``{arm: {task_id: Cell}}`` in order of first appearance."""
    grouped: dict[str, dict[str, list[BenchItem]]] = {}
    for item in items:
        grouped.setdefault(item.arm, {}).setdefault(item.task_id, []).append(item)
    out: dict[str, dict[str, Cell]] = {}
    for arm, tasks in grouped.items():
        cells: dict[str, Cell] = {}
        for task_id, group in tasks.items():
            qualities = [i.metrics.quality for i in group if i.metrics.quality is not None]
            timed = [i.metrics.seconds_to_complete for i in group if i.metrics.latency_valid]
            cells[task_id] = Cell(
                task_id=task_id,
                category=group[0].category,
                difficulty=(difficulties or {}).get(task_id, "unknown"),
                n=len(group),
                scored=len(qualities),
                solved=sum(1 for i in group if i.metrics.quality is not None and i.metrics.solved),
                quality=_mean(qualities),
                quality_sd=stdev(qualities) if len(qualities) >= 2 else None,
                cost=sum(full_cost_usd(i) for i in group),
                seconds=_mean(timed),
                tokens=sum(i.metrics.input_tokens + i.metrics.output_tokens for i in group),
            )
        out[arm] = cells
    return out


# ------------------------------------------------------------------------------- the bootstrap


class Interval(BaseModel):
    """An estimate and its 95% percentile-bootstrap interval, resampling ``n`` tasks."""

    estimate: float
    low: float | None = None  # None: too few tasks, or too few resamples were computable
    high: float | None = None
    n: int = 0

    def text(self, spec: str = ".3f") -> str:
        if self.low is None or self.high is None:
            return f"{self.estimate:{spec}}"
        return f"{self.estimate:{spec}} [{self.low:{spec}}, {self.high:{spec}}]"


def bootstrap_interval[T](
    cells: Sequence[T],
    statistic: Callable[[Sequence[T]], float | None],
    *,
    key: str,
    seed: int = 0,
    n_boot: int = DEFAULT_BOOTSTRAP,
    level: float = 0.95,
) -> Interval | None:
    """``statistic`` of ``cells`` with a task-clustered bootstrap interval.

    Deterministic for a ``seed`` and ``key``. None when the statistic is undefined on the data
    itself; the bounds are None when there are fewer than two tasks or fewer than 90% of the
    resamples could be computed.
    """
    estimate = statistic(cells)
    if estimate is None or not math.isfinite(estimate):
        return None
    n = len(cells)
    interval = Interval(estimate=estimate, n=n)
    if n < 2 or n_boot < 20:
        return interval
    rng = random.Random(f"{seed}:{key}")  # noqa: S311 — reproducible resampling, not security
    draws: list[float] = []
    for _ in range(n_boot):
        value = statistic(rng.choices(cells, k=n))
        if value is not None and math.isfinite(value):
            draws.append(value)
    if len(draws) < _VALID_SHARE * n_boot:
        return interval
    draws.sort()
    tail = (1 - level) / 2
    interval.low = draws[int(tail * len(draws))]
    interval.high = draws[min(int((1 - tail) * len(draws)), len(draws) - 1)]
    return interval


# -- statistics over cells -------------------------------------------------------------------


def _quality(cells: Sequence[Cell]) -> float | None:
    return _mean([c.quality for c in cells if c.quality is not None])


def _solved_rate(cells: Sequence[Cell]) -> float | None:
    return _mean([c.solved / c.scored for c in cells if c.scored])


def _cost_per_task(cells: Sequence[Cell]) -> float | None:
    return _mean([c.cost / c.n for c in cells if c.n])


def _cost_per_solved(cells: Sequence[Cell]) -> float | None:
    solved = sum(c.solved for c in cells)
    return sum(c.cost for c in cells) / solved if solved else None


# ---------------------------------------------------------------------------------- per arm


class ArmStats(BaseModel):
    arm: str
    n_tasks: int
    n_items: int
    errors: int
    halted: int
    solved: int  # solved items
    scored_items: int
    mean_quality: Interval | None = None
    solved_rate: Interval | None = None  # mean over tasks of the share of repeats solved
    cost_per_task: Interval | None = None
    cost_total_usd: float = 0.0
    cost_known: bool = True  # False: some call's price was unknown, so costs are a lower bound
    cost_per_solved: Interval | None = None  # total arm cost / solved items: the headline metric
    seconds_p50: float | None = None  # items replayed from the cache are left out
    seconds_p90: float | None = None
    seconds_mean: float | None = None
    timed_items: int = 0
    output_tokens_per_s: float | None = None  # effective: output tokens / wall seconds
    decode_tokens_per_s: dict[str, float] = Field(default_factory=dict)  # per model, streamed
    ttft_ms: dict[str, float] = Field(default_factory=dict)  # per model, streamed
    tokens_per_solved: float | None = None  # input + output tokens, all repeats / solved items
    quality_per_dollar: float | None = None  # mean quality / cost per task
    quality_per_minute: float | None = None  # mean quality / mean minutes per task
    repeat_sd: float | None = None  # mean over tasks of the spread of quality across repeats
    flip_rate: float | None = None  # tasks whose repeats disagree on solved / tasks with repeats
    eval_cost_usd: float = 0.0  # what scoring and judging cost: never inside the arm's cost
    eval_cost_per_item: float | None = None
    eval_seconds_per_item: float | None = None
    calls_per_item: float | None = None
    cache_hits: int = 0


def _per_model(rows: Sequence[dict[str, float]]) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        for alias, value in row.items():
            grouped[alias].append(value)
    return {alias: fmean(values) for alias, values in sorted(grouped.items())}


def arm_stats(
    arm: str,
    items: Sequence[BenchItem],
    cells: Sequence[Cell],
    *,
    seed: int = 0,
    n_boot: int = DEFAULT_BOOTSTRAP,
) -> ArmStats:
    """The measures of one arm over ``items`` (and the ``cells`` they pool into)."""
    scored = [i for i in items if i.metrics.quality is not None]
    solved = sum(1 for i in scored if i.metrics.solved)
    timed = [i for i in items if i.metrics.latency_valid]
    seconds = [i.metrics.seconds_to_complete for i in timed]
    rates = [
        i.metrics.output_tokens_per_s for i in timed if i.metrics.output_tokens_per_s is not None
    ]
    total_cost = sum(full_cost_usd(i) for i in items)
    tokens = sum(i.metrics.input_tokens + i.metrics.output_tokens for i in items)

    def interval(name: str, fn: Callable[[Sequence[Cell]], float | None]) -> Interval | None:
        return bootstrap_interval(cells, fn, key=f"{arm}:{name}", seed=seed, n_boot=n_boot)

    quality = interval("quality", _quality)
    cost_task = interval("cost_task", _cost_per_task)
    sds = [c.quality_sd for c in cells if c.quality_sd is not None]
    repeated = [c for c in cells if c.scored >= 2]
    mean_seconds = _mean(seconds)
    return ArmStats(
        arm=arm,
        n_tasks=len(cells),
        n_items=len(items),
        errors=sum(1 for i in items if i.status == "error"),
        halted=sum(1 for i in items if i.status == "halted"),
        solved=solved,
        scored_items=len(scored),
        mean_quality=quality,
        solved_rate=interval("solved", _solved_rate),
        cost_per_task=cost_task,
        cost_total_usd=total_cost,
        cost_known=all(i.metrics.cost_known for i in items),
        cost_per_solved=interval("cost_solved", _cost_per_solved),
        seconds_p50=percentile(seconds, 0.5),
        seconds_p90=percentile(seconds, 0.9),
        seconds_mean=mean_seconds,
        timed_items=len(timed),
        output_tokens_per_s=_mean(rates),
        decode_tokens_per_s=_per_model([i.metrics.decode_tokens_per_s for i in timed]),
        ttft_ms=_per_model([i.metrics.ttft_ms for i in timed]),
        tokens_per_solved=tokens / solved if solved else None,
        quality_per_dollar=(
            quality.estimate / cost_task.estimate
            if quality and cost_task and cost_task.estimate > 0
            else None
        ),
        quality_per_minute=(
            quality.estimate / (mean_seconds / 60) if quality and mean_seconds else None
        ),
        repeat_sd=_mean(sds),
        flip_rate=(
            sum(1 for c in repeated if 0 < c.solved < c.scored) / len(repeated)
            if repeated
            else None
        ),
        eval_cost_usd=sum(i.metrics.eval_cost_usd for i in items),
        eval_cost_per_item=_mean([i.metrics.eval_cost_usd for i in items]),
        eval_seconds_per_item=_mean([i.metrics.eval_seconds for i in items]),
        calls_per_item=_mean([float(i.metrics.calls) for i in items]),
        cache_hits=sum(i.metrics.cache_hits for i in items),
    )


# ------------------------------------------------------------------------------ the frontier


def pareto_frontier(points: dict[str, tuple[float, float]]) -> list[str]:
    """Names of the points no other point beats: ``(cost, quality)``, lower cost and higher
    quality are better. A point is dominated when another is at least as cheap and at least as
    good and strictly better in one; identical points are both kept. Cheapest first."""
    frontier = [
        name
        for name, (cost, quality) in points.items()
        if not any(
            (c <= cost and q >= quality) and (c < cost or q > quality)
            for other, (c, q) in points.items()
            if other != name
        )
    ]
    return sorted(frontier, key=lambda n: (points[n][0], -points[n][1], n))


# ----------------------------------------------------------------------------- comparisons


class PairedQuality(BaseModel):
    """Quality of one arm minus another's, on the tasks both answered."""

    n_tasks: int
    difference: Interval | None = None  # mean over tasks of (A - B)
    wins: int = 0
    ties: int = 0
    losses: int = 0
    sign_test_p: float | None = None
    effect_size: float | None = None  # mean difference / its spread across tasks (Cohen's d_z)


class Verdict(BaseModel):
    claim: Claim
    outcome: Outcome
    reason: str


class Comparison(BaseModel):
    """Arm ``challenger`` against arm ``baseline`` over one scope (all tasks or one category)."""

    challenger: str
    baseline: str
    scope: str  # "all" or a category
    n_tasks: int  # tasks both arms were scored on
    quality: PairedQuality
    cost_per_solved_ratio: Interval | None = None  # challenger / baseline; below 1: cheaper
    cost_per_task_ratio: Interval | None = None
    seconds_ratio: Interval | None = None  # mean seconds per task; below 1: faster
    verdicts: list[Verdict] = Field(default_factory=list)


@dataclass(frozen=True)
class Rules:
    """How strong the evidence must be before a verdict says yes or no."""

    margin: float = DEFAULT_MARGIN  # quality an arm may lose and still be "not worse"
    min_tasks: int = DEFAULT_MIN_TASKS
    tie: float = DEFAULT_TIE
    n_boot: int = DEFAULT_BOOTSTRAP
    seed: int = 0


@dataclass(frozen=True)
class _Pair:
    a: Cell
    b: Cell


def _gap(pair: _Pair) -> float:
    """Quality of ``a`` minus ``b`` (pairs are built from cells that both have one)."""
    assert pair.a.quality is not None and pair.b.quality is not None  # noqa: S101
    return pair.a.quality - pair.b.quality


def _paired_quality(pairs: Sequence[_Pair]) -> float | None:
    return _mean([_gap(p) for p in pairs])


def _ratio(num: float | None, den: float | None) -> float | None:
    return num / den if num is not None and den else None


def _pair_cost_per_solved(pairs: Sequence[_Pair]) -> float | None:
    return _ratio(_cost_per_solved([p.a for p in pairs]), _cost_per_solved([p.b for p in pairs]))


def _pair_cost_per_task(pairs: Sequence[_Pair]) -> float | None:
    return _ratio(_cost_per_task([p.a for p in pairs]), _cost_per_task([p.b for p in pairs]))


def _pair_seconds(pairs: Sequence[_Pair]) -> float | None:
    timed = [p for p in pairs if p.a.seconds is not None and p.b.seconds is not None]
    return _ratio(
        _mean([p.a.seconds for p in timed if p.a.seconds is not None]),
        _mean([p.b.seconds for p in timed if p.b.seconds is not None]),
    )


def compare_arms(
    challenger: str,
    baseline: str,
    a_cells: dict[str, Cell],
    b_cells: dict[str, Cell],
    *,
    scope: str = "all",
    rules: Rules | None = None,
) -> Comparison:
    """Paired comparison on the tasks both arms were scored on (verdicts are added separately)."""
    rules = rules or Rules()
    pairs = [
        _Pair(a_cells[t], b_cells[t])
        for t in a_cells
        if t in b_cells and a_cells[t].quality is not None and b_cells[t].quality is not None
    ]
    diffs = [_gap(p) for p in pairs]
    wins = sum(1 for d in diffs if d > rules.tie)
    losses = sum(1 for d in diffs if d < -rules.tie)
    spread = stdev(diffs) if len(diffs) >= 2 else None
    mean_diff = _mean(diffs)
    effect: float | None = None
    if spread:
        effect = (mean_diff or 0.0) / spread

    def interval(name: str, fn: Callable[[Sequence[_Pair]], float | None]) -> Interval | None:
        return bootstrap_interval(
            pairs,
            fn,
            key=f"{challenger}>{baseline}:{scope}:{name}",
            seed=rules.seed,
            n_boot=rules.n_boot,
        )

    return Comparison(
        challenger=challenger,
        baseline=baseline,
        scope=scope,
        n_tasks=len(pairs),
        quality=PairedQuality(
            n_tasks=len(pairs),
            difference=interval("quality", _paired_quality),
            wins=wins,
            ties=len(diffs) - wins - losses,
            losses=losses,
            sign_test_p=sign_test_p(wins, losses),
            effect_size=effect,
        ),
        cost_per_solved_ratio=interval("cost_solved", _pair_cost_per_solved),
        cost_per_task_ratio=interval("cost_task", _pair_cost_per_task),
        seconds_ratio=interval("seconds", _pair_seconds),
    )


# --------------------------------------------------------------------------------- verdicts


def _ratio_verdict(claim: Claim, what: str, ratio: Interval | None, undefined: str) -> Verdict:
    """cheaper / faster: yes when the ratio's whole interval is below 1, no when it is above."""
    if ratio is None:
        return Verdict(claim=claim, outcome="inconclusive", reason=undefined)
    if ratio.low is None or ratio.high is None:
        return Verdict(
            claim=claim,
            outcome="inconclusive",
            reason=f"{what} ratio is {ratio.estimate:.2f}; too few tasks for an interval",
        )
    text = f"{what} ratio {ratio.text('.2f')} (below 1 is better)"
    if ratio.high < 1:
        return Verdict(claim=claim, outcome="yes", reason=f"{text}: the whole interval is below 1")
    if ratio.low >= 1:
        return Verdict(
            claim=claim, outcome="no", reason=f"{text}: the whole interval is at or above 1"
        )
    return Verdict(claim=claim, outcome="inconclusive", reason=f"{text}: the interval includes 1")


def verdicts(comparison: Comparison, rules: Rules, *, blocked: str | None = None) -> list[Verdict]:
    """The four claims for ``comparison.challenger`` relative to ``comparison.baseline``.

    * cheaper: cost per solved task, ratio below 1 across its whole interval;
    * faster: mean seconds per task, same test;
    * not worse: the interval of the quality difference stays above ``-margin`` (non-inferiority);
      *no* when it lies wholly below ``-margin``;
    * better: the interval of the quality difference is wholly above 0; *no* when wholly at or
      below 0.

    Below ``rules.min_tasks`` paired tasks nothing is claimed. ``blocked`` (a reason) withholds
    every verdict, as when the judges behind the scores are not shown to be reliable.
    """
    if blocked is not None:
        return [Verdict(claim=c, outcome="blocked", reason=blocked) for c in CLAIMS]
    if comparison.n_tasks < rules.min_tasks:
        reason = (
            f"only {comparison.n_tasks} task(s) answered by both arms; "
            f"at least {rules.min_tasks} are needed to claim anything"
        )
        return [Verdict(claim=c, outcome="inconclusive", reason=reason) for c in CLAIMS]
    diff = comparison.quality.difference
    out = [
        _ratio_verdict(
            "cheaper",
            "cost per solved task",
            comparison.cost_per_solved_ratio,
            "an arm solved no task, so its cost per solved task is undefined",
        ),
        _ratio_verdict(
            "faster",
            "seconds per task",
            comparison.seconds_ratio,
            "no task has a speed measurement for both arms (cache replays are not measurements)",
        ),
    ]
    if diff is None or diff.low is None or diff.high is None:
        reason = "quality difference has no interval"
        out += [
            Verdict(claim="not_worse", outcome="inconclusive", reason=reason),
            Verdict(claim="better", outcome="inconclusive", reason=reason),
        ]
        return out
    text = f"quality difference {diff.text('+.3f')} over {diff.n} tasks"
    margin = rules.margin
    if diff.low > -margin:
        nw = Verdict(
            claim="not_worse",
            outcome="yes",
            reason=f"{text}: the lower bound is above -{margin:g}, the non-inferiority margin",
        )
    elif diff.high < -margin:
        nw = Verdict(
            claim="not_worse",
            outcome="no",
            reason=f"{text}: the upper bound is below -{margin:g}: it loses more than the margin",
        )
    else:
        nw = Verdict(
            claim="not_worse",
            outcome="inconclusive",
            reason=f"{text}: the interval straddles the -{margin:g} margin",
        )
    if diff.low > 0:
        better = Verdict(claim="better", outcome="yes", reason=f"{text}: the interval is above 0")
    elif diff.high <= 0:
        better = Verdict(
            claim="better", outcome="no", reason=f"{text}: the interval is at or below 0"
        )
    else:
        better = Verdict(
            claim="better", outcome="inconclusive", reason=f"{text}: the interval includes 0"
        )
    return [*out, nw, better]


# ---------------------------------------------------------------------------------- the study


class StudyStats(BaseModel):
    baseline: str
    arms: list[ArmStats]
    by_category: dict[str, list[ArmStats]] = Field(default_factory=dict)
    by_difficulty: dict[str, list[ArmStats]] = Field(default_factory=dict)
    pareto_cost: list[str] = Field(default_factory=list)  # arms on the cost-quality frontier
    pareto_latency: list[str] = Field(default_factory=list)  # ... and the latency-quality one
    comparisons: list[Comparison] = Field(default_factory=list)  # every arm vs the baseline
    margin: float = DEFAULT_MARGIN
    min_tasks: int = DEFAULT_MIN_TASKS
    tie: float = DEFAULT_TIE
    n_boot: int = DEFAULT_BOOTSTRAP
    blocked: str | None = None  # why the headline verdicts were withheld


def default_baseline(arms: Sequence[str]) -> str:
    """``solo-frontier`` when the study has it (the arm Fusion must beat), else the first arm."""
    return "solo-frontier" if "solo-frontier" in arms else arms[0]


def _frontier(stats: Sequence[ArmStats], x: Callable[[ArmStats], float | None]) -> list[str]:
    points = {
        s.arm: (cost, s.mean_quality.estimate)
        for s in stats
        if s.mean_quality is not None and (cost := x(s)) is not None
    }
    return pareto_frontier(points)


def study_stats(
    items: Sequence[BenchItem],
    *,
    difficulties: dict[str, str] | None = None,
    baseline: str | None = None,
    rules: Rules | None = None,
    blocked: str | None = None,
    blocked_categories: frozenset[str] = frozenset(),
) -> StudyStats:
    """Everything the reports show, from a run's items.

    ``blocked`` withholds every verdict (with that reason); ``blocked_categories`` withholds the
    verdicts of those categories only (the ones whose scores a judge decided).
    """
    rules = rules or Rules()
    cells = build_cells(items, difficulties)
    arms = list(cells)
    if not arms:
        msg = "the run has no items to report on"
        raise ValueError(msg)
    base = baseline or default_baseline(arms)
    if base not in cells:
        msg = f"baseline arm '{base}' is not in the run (arms: {', '.join(arms)})"
        raise ValueError(msg)
    by_arm: dict[str, list[BenchItem]] = {a: [] for a in arms}
    for item in items:
        by_arm[item.arm].append(item)

    def stats_for(
        select: Callable[[BenchItem], bool], cell_ok: Callable[[Cell], bool]
    ) -> list[ArmStats]:
        out = []
        for arm in arms:
            chosen = [i for i in by_arm[arm] if select(i)]
            if chosen:
                pooled = [c for c in cells[arm].values() if cell_ok(c)]
                out.append(arm_stats(arm, chosen, pooled, seed=rules.seed, n_boot=rules.n_boot))
        return out

    overall = stats_for(lambda i: True, lambda c: True)
    categories = sorted({c.category for arm in cells.values() for c in arm.values()})
    levels = sorted({c.difficulty for arm in cells.values() for c in arm.values()})
    by_category = {
        cat: stats_for(lambda i, cat=cat: i.category == cat, lambda c, cat=cat: c.category == cat)  # type: ignore[misc]
        for cat in categories
    }
    by_difficulty = (
        {}
        if levels == ["unknown"]
        else {
            lv: stats_for(
                lambda i, lv=lv: (difficulties or {}).get(i.task_id, "unknown") == lv,  # type: ignore[misc]
                lambda c, lv=lv: c.difficulty == lv,  # type: ignore[misc]
            )
            for lv in levels
        }
    )

    comparisons: list[Comparison] = []
    scopes = [("all", None), *((cat, cat) for cat in categories)]
    for arm in arms:
        if arm == base:
            continue
        for scope, cat in scopes:

            def pick(cs: dict[str, Cell], cat: str | None = cat) -> dict[str, Cell]:
                return {t: c for t, c in cs.items() if cat is None or c.category == cat}

            comparison = compare_arms(
                arm, base, pick(cells[arm]), pick(cells[base]), scope=scope, rules=rules
            )
            why = blocked
            if why is None and (scope in blocked_categories):
                why = f"the {scope} scores were decided by judges that are not shown to be reliable"
            comparison.verdicts = verdicts(comparison, rules, blocked=why)
            comparisons.append(comparison)

    return StudyStats(
        baseline=base,
        arms=overall,
        by_category=by_category,
        by_difficulty=by_difficulty,
        pareto_cost=_frontier(
            overall, lambda s: s.cost_per_task.estimate if s.cost_per_task else None
        ),
        pareto_latency=_frontier(overall, lambda s: s.seconds_p50),
        comparisons=comparisons,
        margin=rules.margin,
        min_tasks=rules.min_tasks,
        tie=rules.tie,
        n_boot=rules.n_boot,
        blocked=blocked,
    )
