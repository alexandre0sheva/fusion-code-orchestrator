"""Probe: run one prompt pack on a slice of the dev split and compare it with another, task by task.

A probe is an ordinary ``fusion bench`` run of one arm, made while the pack is active, so the
response cache, the run's ``max_usd`` and the live-spend ledger all apply to it. Two probes are
compared on the tasks both scored: the effect is the mean per-task quality difference with a
task-clustered bootstrap interval, and the **noise band** is the larger of half that interval and
``min_gain``. An edit ``helped`` when the effect clears the band, ``hurt`` when it falls below
minus the band, and is ``confounded`` when it clearly helped one task type and clearly hurt another,
each with at least ``min_category_tasks`` tasks (or when jobs failed, so the two probes rest on
different tasks): the effect cannot be traced to the edit as a whole.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Sequence
from statistics import fmean

from pydantic import BaseModel

from fusion.bench.runner import BenchEnv, BenchRun, run_bench
from fusion.bench.spec import Arm, BenchConfig, BenchTask
from fusion.bench.stats import bootstrap_interval
from fusion.config.prompt_pack import PromptPack, use_prompt_pack
from fusion.library.store import Verdict

__all__ = ["MIN_CATEGORY_TASKS", "Effect", "Probe", "compare", "probe", "split_holdout"]

MIN_CATEGORY_TASKS = 2  # compared tasks a task type needs before its effect can confound an edit


class Probe(BaseModel):
    """One pack measured on one slice: per-task mean quality and solved share over repeats."""

    label: str
    run_id: str
    pack_digest: str
    status: str
    stop_reason: str | None = None
    quality: dict[str, float]  # task id -> mean quality (a task with no scored item is absent)
    solved: dict[str, float]
    categories: dict[str, str]  # task id -> category
    cost_usd: float = 0.0
    failed: int = 0  # jobs that ended in an error

    @property
    def stopped(self) -> bool:
        return self.status != "completed"

    def mean(self) -> float:
        return fmean(self.quality.values()) if self.quality else 0.0

    def by_category(self) -> dict[str, tuple[int, float, float]]:
        """``{category: (tasks, mean quality, solved share)}``, what a proposer is shown."""
        groups: dict[str, list[str]] = defaultdict(list)
        for task_id in self.quality:
            groups[self.categories[task_id]].append(task_id)
        return {
            cat: (
                len(ids),
                fmean(self.quality[i] for i in ids),
                fmean(self.solved.get(i, 0.0) for i in ids),
            )
            for cat, ids in groups.items()
        }


class Effect(BaseModel):
    """How a candidate pack did against a reference pack on the same tasks."""

    delta: float  # mean per-task quality difference, candidate minus reference
    low: float | None = None  # 95% bootstrap interval of ``delta``
    high: float | None = None
    band: float  # the noise band ``delta`` must clear
    n: int  # tasks compared
    by_category: dict[str, float]
    improved: int = 0  # tasks whose quality went up by more than the band
    worsened: int = 0
    verdict: Verdict
    reason: str

    def text(self) -> str:
        interval = (
            f" [{self.low:+.3f}, {self.high:+.3f}]"
            if self.low is not None and self.high is not None
            else ""
        )
        return f"{self.delta:+.3f}{interval}"


def split_holdout(
    tasks: Sequence[BenchTask], share: float, seed: int = 0
) -> tuple[list[BenchTask], list[BenchTask]]:
    """``(tuning tasks, held-out tasks)``: a seeded share of each category is held out (at least
    one where a category has two or more tasks). Both keep the dataset's order."""
    rng = random.Random(f"tune-holdout:{seed}")  # noqa: S311 — reproducible sampling
    by_cat: dict[str, list[BenchTask]] = defaultdict(list)
    for task in tasks:
        by_cat[task.category].append(task)
    held: set[str] = set()
    for _, group in sorted(by_cat.items()):
        if len(group) < 2 or share <= 0:
            continue
        count = min(max(round(share * len(group)), 1), len(group) - 1)
        held.update(t.id for t in rng.sample(group, count))
    return [t for t in tasks if t.id not in held], [t for t in tasks if t.id in held]


async def probe(
    pack: PromptPack,
    tasks: list[BenchTask],
    *,
    env: BenchEnv,
    cfg: BenchConfig,
    label: str,
    run_id: str,
) -> Probe:
    """Run ``cfg``'s single arm on ``tasks`` with ``pack`` active."""
    with use_prompt_pack(pack):
        run = await run_bench(cfg, env=env, run_id=run_id, tasks=tasks)
    return _summarize(run, pack, tasks, label)


def _summarize(run: BenchRun, pack: PromptPack, tasks: list[BenchTask], label: str) -> Probe:
    qualities: dict[str, list[float]] = defaultdict(list)
    solved: dict[str, list[float]] = defaultdict(list)
    for item in run.items:
        if item.status == "error":
            continue
        # A halted answer counts at its score (0 if none): stopping short is the arm's doing.
        qualities[item.task_id].append(item.metrics.quality or 0.0)
        solved[item.task_id].append(1.0 if item.metrics.solved else 0.0)
    return Probe(
        label=label,
        run_id=run.run_id,
        pack_digest=pack.digest,
        status=run.status,
        stop_reason=run.stop_reason,
        quality={t: fmean(v) for t, v in qualities.items()},
        solved={t: fmean(v) for t, v in solved.items()},
        categories={t.id: t.category for t in tasks},
        cost_usd=run.spent_usd + run.eval_spent_usd,
        failed=run.failed_jobs,
    )


def compare(
    reference: Probe,
    candidate: Probe,
    *,
    min_gain: float,
    seed: int = 0,
    min_category_tasks: int = MIN_CATEGORY_TASKS,
) -> Effect:
    """The candidate's effect, its noise band and its verdict (see the module docstring).

    Only task types with at least ``min_category_tasks`` compared tasks can make an edit
    ``confounded``: one task moving either way is noise, not a task-type effect.
    """
    shared = sorted(set(reference.quality) & set(candidate.quality))
    diffs = [candidate.quality[t] - reference.quality[t] for t in shared]
    if not diffs:
        return Effect(
            delta=0.0,
            band=min_gain,
            n=0,
            by_category={},
            verdict="confounded",
            reason="no task was scored in both probes",
        )
    interval = bootstrap_interval(
        diffs, lambda xs: fmean(xs) if xs else None, key=f"tune:{candidate.label}", seed=seed
    )
    delta = fmean(diffs)
    low = interval.low if interval else None
    high = interval.high if interval else None
    half = (high - low) / 2 if low is not None and high is not None else 0.0
    band = max(min_gain, half)
    per_cat: dict[str, list[float]] = defaultdict(list)
    for task_id, diff in zip(shared, diffs, strict=True):
        per_cat[reference.categories[task_id]].append(diff)
    by_category = {cat: fmean(values) for cat, values in sorted(per_cat.items())}
    counted = {c for c, values in per_cat.items() if len(values) >= min_category_tasks}
    up = [c for c, d in by_category.items() if d > band and c in counted]
    down = [c for c, d in by_category.items() if d < -band and c in counted]
    # Task types that moved beyond the band but are too small to say so: named, not counted.
    thin = [c for c, d in by_category.items() if abs(d) > band and c not in counted]
    verdict: Verdict
    if reference.failed or candidate.failed:
        failed = reference.failed + candidate.failed
        verdict = "confounded"
        reason = f"{failed} job(s) failed, so the probes rest on different tasks"
    elif up and down:
        verdict, reason = "confounded", f"helped {', '.join(up)} but hurt {', '.join(down)}"
    elif delta > band:
        verdict, reason = "helped", f"{delta:+.3f} clears the noise band {band:.3f}"
    elif delta < -band:
        verdict, reason = "hurt", f"{delta:+.3f} is below the noise band -{band:.3f}"
    else:
        verdict, reason = "no_effect", f"{delta:+.3f} is within the noise band {band:.3f}"
    if thin and verdict != "confounded":
        reason += (
            f"; {', '.join(thin)} moved but had too few tasks "
            f"(under {min_category_tasks}) to count as a task-type effect"
        )
    return Effect(
        delta=delta,
        low=low,
        high=high,
        band=band,
        n=len(shared),
        by_category=by_category,
        improved=sum(1 for d in diffs if d > band),
        worsened=sum(1 for d in diffs if d < -band),
        verdict=verdict,
        reason=reason,
    )


def probe_config(
    *,
    dataset: object,
    arm: str,
    repeats: int,
    seed: int,
    max_usd: float,
    mock: bool,
    cache: bool,
    concurrency: int,
    spend_stop_usd: float | None,
) -> BenchConfig:
    """The bench config of one probe: one arm, the dev split, the tasks given to ``probe``."""
    return BenchConfig.model_validate(
        {
            "dataset": dataset,
            "arms": [Arm(name=arm, strategy=arm)],
            "repeats": repeats,
            "seed": seed,
            "max_usd": max_usd,
            "mock": mock,
            "cache": cache,
            "concurrency": concurrency,
            "split": "dev",
            "spend_stop_usd": spend_stop_usd,
        }
    )
