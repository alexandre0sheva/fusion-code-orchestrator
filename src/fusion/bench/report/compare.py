"""Before and after: the same arm in two runs (a tuning change, a new catalog price, a fix).

Both runs are cut down to the tasks they share and each arm is compared with itself, paired by
task, so the difference is the change and not a different sample of tasks.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from fusion.bench.meta import RunMeta
from fusion.bench.report.fmt import (
    CLAIM_TEXT,
    OUTCOME_SYMBOL,
    OUTCOME_WORD,
    interval_text,
)
from fusion.bench.stats import (
    CLAIMS,
    ArmStats,
    Comparison,
    Interval,
    Rules,
    arm_stats,
    build_cells,
    compare_arms,
    verdicts,
)
from fusion.bench.store import BenchItem, BenchRunRecord

__all__ = ["ArmChange", "RunComparison", "compare_runs", "render_comparison"]


class ArmChange(BaseModel):
    arm: str
    n_tasks: int  # tasks both runs scored this arm on
    before: ArmStats
    after: ArmStats
    comparison: Comparison  # after (challenger) against before (baseline)


class RunComparison(BaseModel):
    before: str
    after: str
    changes: list[ArmChange] = Field(default_factory=list)
    only_before: list[str] = Field(default_factory=list)  # arms the second run lacks
    only_after: list[str] = Field(default_factory=list)
    shared_tasks: int = 0
    notes: list[str] = Field(default_factory=list)
    margin: float
    min_tasks: int


def compare_runs(
    before: tuple[BenchRunRecord, list[BenchItem], RunMeta | None],
    after: tuple[BenchRunRecord, list[BenchItem], RunMeta | None],
    *,
    rules: Rules | None = None,
) -> RunComparison:
    """Each arm the two runs share, ``after`` against ``before``, on the tasks they share."""
    rules = rules or Rules()
    (rec_a, items_a, meta_a), (rec_b, items_b, meta_b) = before, after
    arms_a = list(dict.fromkeys(i.arm for i in items_a))
    arms_b = list(dict.fromkeys(i.arm for i in items_b))
    tasks_a = {i.task_id for i in items_a}
    shared = sorted(tasks_a & {i.task_id for i in items_b})
    out = RunComparison(
        before=rec_a.run_id,
        after=rec_b.run_id,
        only_before=[a for a in arms_a if a not in arms_b],
        only_after=[a for a in arms_b if a not in arms_a],
        shared_tasks=len(shared),
        margin=rules.margin,
        min_tasks=rules.min_tasks,
    )
    if rec_a.mock != rec_b.mock:
        out.notes.append(
            "One run is simulated and the other is not: the numbers are not comparable."
        )
    if meta_a and meta_b and meta_a.dataset_hash != meta_b.dataset_hash:
        out.notes.append(
            f"The runs used different datasets ({meta_a.dataset_hash} and {meta_b.dataset_hash}); "
            f"only the {len(shared)} task(s) in both are compared."
        )
    if meta_a is None or meta_b is None:
        out.notes.append("A run has no dataset snapshot; tasks are matched by id only.")
    if rec_a.config.repeats != rec_b.config.repeats:
        out.notes.append(
            f"Repeats differ ({rec_a.config.repeats} and {rec_b.config.repeats}): per-task means "
            "are compared, so the noise in each differs."
        )
    if not shared:
        out.notes.append("The runs share no task: nothing to compare.")
        return out
    keep = set(shared)
    ca = build_cells([i for i in items_a if i.task_id in keep])
    cb = build_cells([i for i in items_b if i.task_id in keep])
    for arm in (a for a in arms_a if a in arms_b):
        comparison = compare_arms(arm, arm, cb[arm], ca[arm], rules=rules)
        comparison.verdicts = verdicts(comparison, rules)
        pick_a = [i for i in items_a if i.arm == arm and i.task_id in keep]
        pick_b = [i for i in items_b if i.arm == arm and i.task_id in keep]
        out.changes.append(
            ArmChange(
                arm=arm,
                n_tasks=comparison.n_tasks,
                before=arm_stats(
                    arm, pick_a, list(ca[arm].values()), seed=rules.seed, n_boot=rules.n_boot
                ),
                after=arm_stats(
                    arm, pick_b, list(cb[arm].values()), seed=rules.seed, n_boot=rules.n_boot
                ),
                comparison=comparison,
            )
        )
    return out


def _change(before: float | None, after: float | None, spec: str) -> str:
    if before is None or after is None:
        return "-"
    return f"{before:{spec}} → {after:{spec}}"


def _estimate(value: Interval | None) -> float | None:
    return None if value is None else value.estimate


def render_comparison(result: RunComparison) -> str:
    """Markdown. Verdicts read as claims about the second run: it is cheaper, faster, ..."""
    out = [f"# Before and after: {result.before} → {result.after}", ""]
    out += [f"> {n}" for n in result.notes] + ([""] if result.notes else [])
    out += [
        f"{result.shared_tasks} task(s) in both runs. Each arm is compared with itself, paired by "
        f"task; verdicts say whether `{result.after}` is cheaper, faster, not worse (margin "
        f"{result.margin:g}) or better than `{result.before}`.",
        "",
        "| Arm | Tasks | Quality | Δ quality (95% CI) | $/solved | p50 s "
        "| Cheaper | Faster | Not worse | Better |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in result.changes:
        by_claim = {v.claim: v for v in c.comparison.verdicts}
        cells = [
            c.arm,
            str(c.n_tasks),
            _change(_estimate(c.before.mean_quality), _estimate(c.after.mean_quality), ".3f"),
            interval_text(c.comparison.quality.difference, "+.3f"),
            _change(_estimate(c.before.cost_per_solved), _estimate(c.after.cost_per_solved), ".4f"),
            _change(c.before.seconds_p50, c.after.seconds_p50, ".1f"),
            *(
                f"{OUTCOME_SYMBOL[by_claim[k].outcome]} {OUTCOME_WORD[by_claim[k].outcome]}"
                for k in CLAIMS
            ),
        ]
        out.append("| " + " | ".join(cells) + " |")
    out.append("")
    for c in result.changes:
        out += [
            f"- **{c.arm}**: {CLAIM_TEXT[v.claim]}: {OUTCOME_WORD[v.outcome]}. {v.reason}."
            for v in c.comparison.verdicts
        ]
    if result.only_before:
        out += ["", f"Only in {result.before}: {', '.join(result.only_before)}."]
    if result.only_after:
        out += ["", f"Only in {result.after}: {', '.join(result.only_after)}."]
    out.append("")
    return "\n".join(out)
