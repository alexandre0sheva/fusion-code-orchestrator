"""Number and verdict formatting shared by the Markdown and HTML reports."""

from __future__ import annotations

from fusion.bench.report.data import Report
from fusion.bench.stats import Comparison, Interval, Verdict

__all__ = [
    "CLAIM_TEXT",
    "OUTCOME_SYMBOL",
    "OUTCOME_WORD",
    "comparison_for",
    "interval_text",
    "methodology",
    "money",
    "pct",
    "seconds",
    "verdict_line",
]

CLAIM_TEXT = {
    "cheaper": "cheaper (per solved task)",
    "faster": "faster",
    "not_worse": "not worse",
    "better": "better",
}
OUTCOME_SYMBOL = {"yes": "✓", "no": "✗", "inconclusive": "?", "blocked": "⊘"}
OUTCOME_WORD = {
    "yes": "yes",
    "no": "no",
    "inconclusive": "inconclusive",
    "blocked": "blocked",
}


def money(value: float | None) -> str:
    if value is None:
        return "-"
    if value == 0:
        return "$0"
    return f"${value:.4f}" if abs(value) < 1 else f"${value:,.2f}"


def pct(value: float | None, digits: int = 0) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def seconds(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}s" if value < 100 else f"{value:.0f}s"


def interval_text(value: Interval | None, spec: str = ".3f", *, bounds: bool = True) -> str:
    """``0.712 [0.65, 0.77]``; just the estimate when there are no bounds or ``bounds`` is off."""
    if value is None:
        return "-"
    if not bounds or value.low is None or value.high is None:
        return f"{value.estimate:{spec}}"
    return f"{value.estimate:{spec}} [{value.low:{spec}}, {value.high:{spec}}]"


def verdict_line(comparison: Comparison, verdict: Verdict) -> str:
    """One sentence: who against whom, where, what, and why."""
    return (
        f"{comparison.challenger} vs {comparison.baseline} on {comparison.scope} "
        f"({comparison.n_tasks} tasks): {CLAIM_TEXT[verdict.claim]}: "
        f"{OUTCOME_WORD[verdict.outcome]}. {verdict.reason}."
    )


def comparison_for(
    comparisons: list[Comparison], challenger: str, scope: str = "all"
) -> Comparison | None:
    return next(
        (c for c in comparisons if c.challenger == challenger and c.scope == scope),
        None,
    )


def methodology(report: Report) -> list[tuple[str, str]]:
    """The footer's facts, as ``(label, text)``: what a reader needs to reproduce or doubt the
    study. Printed by every format."""
    run, meta, stats = report.run, report.meta, report.stats
    rows: list[tuple[str, str]] = [
        ("Run", f"{run.run_id} ({run.status}), created {run.created_at}")
    ]
    if meta is not None:
        origin = (
            " (read from today's files, not captured at the start)" if meta.reconstructed else ""
        )
        rows.append(
            (
                "Dataset",
                f"{meta.dataset}, split {meta.split}, {meta.task_count} tasks, "
                f"hash {meta.dataset_hash}{origin}",
            )
        )
    else:
        rows.append(("Dataset", f"{run.dataset} (no snapshot of its contents)"))
    for arm in meta.arms if meta else []:
        extra = f", overrides {arm.overrides}" if arm.overrides else ""
        models = ", ".join(arm.members) or "-"
        synth = f", synthesizer {arm.aggregator_model}" if arm.aggregator_model else ""
        rows.append(
            (
                f"Arm {arm.name}",
                f"strategy {arm.strategy}{extra}: {arm.kind}, models {models}{synth}, "
                f"aggregator {arm.aggregator}, rounds {arm.rounds}, judge {arm.judge}",
            )
        )
    for model in meta.models.values() if meta else []:
        price = (
            f"${model.input_per_1m:g} in / ${model.output_per_1m:g} out per 1M tokens, "
            f"verified {model.verified_on or 'never'}"
            if model.input_per_1m is not None
            else "price unknown"
        )
        rows.append((f"Model {model.alias}", f"{model.provider} {model.model_id}: {price}"))
    rows += [
        ("Judge models", ", ".join(run.judge_models) or "none (deterministic scorers only)"),
        ("Repeats / seed", f"{run.repeats} per task / seed {run.seed}"),
        (
            "Intervals",
            f"95% percentile bootstrap, {stats.n_boot} resamples of tasks (repeats are not "
            "independent); comparisons are paired on the tasks both arms were scored on",
        ),
        (
            "Verdict rules",
            f"non-inferiority margin {stats.margin:g} quality; at least {stats.min_tasks} paired "
            f"tasks; a task is a tie when the arms differ by at most {stats.tie:g}; baseline "
            f"{stats.baseline}",
        ),
        (
            "Spend",
            f"{money(run.spent_usd)} on the arms + {money(run.eval_spent_usd)} on scoring and "
            f"judging (cap {money(run.max_usd)})"
            + (
                f"; cumulative live spend {money(report.spend_total_usd)}"
                if report.spend_total_usd is not None and not run.mock
                else ""
            ),
        ),
    ]
    if meta is not None:
        rows.append(("Fusion version", meta.version))
    return rows
