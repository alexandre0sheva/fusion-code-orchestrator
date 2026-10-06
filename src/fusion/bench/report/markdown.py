"""The Markdown report: for a README, a docs page or a terminal pager. No images; tables only."""

from __future__ import annotations

from fusion.bench.report.data import Example, ExampleSide, Report
from fusion.bench.report.fmt import (
    CLAIM_TEXT,
    OUTCOME_SYMBOL,
    OUTCOME_WORD,
    comparison_for,
    interval_text,
    methodology,
    money,
    pct,
    seconds,
    verdict_line,
)
from fusion.bench.stats import CLAIMS, ArmStats, Comparison

__all__ = ["render_markdown"]


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return [*out, ""]


def _mark(report: Report, arm: str) -> str:
    stats = report.stats
    tags = [
        t
        for t, frontier in (("$", stats.pareto_cost), ("s", stats.pareto_latency))
        if arm in frontier
    ]
    return " ".join(tags) or "-"


def _arm_rows(arms: list[ArmStats], report: Report | None = None) -> list[list[str]]:
    return [
        [
            a.arm,
            str(a.n_tasks),
            interval_text(a.mean_quality, ".3f"),
            pct(a.solved_rate.estimate if a.solved_rate else None),
            money(a.cost_per_task.estimate if a.cost_per_task else None),
            interval_text(a.cost_per_solved, ".4f", bounds=False) if a.cost_per_solved else "-",
            seconds(a.seconds_p50),
            seconds(a.seconds_p90),
            *([_mark(report, a.arm)] if report else []),
        ]
        for a in arms
    ]


_ARM_HEADER = ["Arm", "Tasks", "Quality (95% CI)", "Solved", "$/task", "$/solved", "p50", "p90"]


def _headline(report: Report) -> list[str]:
    stats = report.stats
    out = [
        "## Headline",
        "",
        f"Each arm against **{stats.baseline}**, on all tasks. "
        "✓ yes, ✗ no, ? inconclusive, ⊘ blocked. The reasons are listed below the table.",
        "",
    ]
    rows: list[list[str]] = []
    reasons: list[str] = []
    for arm in stats.arms:
        cmp = comparison_for(stats.comparisons, arm.arm)
        if cmp is None:
            continue
        by_claim = {v.claim: v for v in cmp.verdicts}
        rows.append(
            [
                arm.arm,
                str(cmp.n_tasks),
                *(
                    f"{OUTCOME_SYMBOL[by_claim[c].outcome]} {OUTCOME_WORD[by_claim[c].outcome]}"
                    for c in CLAIMS
                ),
            ]
        )
        if len({v.reason for v in cmp.verdicts}) == 1:  # one reason for all four claims
            why = cmp.verdicts[0].reason
            reasons.append(f"- {cmp.challenger} vs {cmp.baseline} ({cmp.n_tasks} tasks): {why}.")
        else:
            reasons += [f"- {verdict_line(cmp, v)}" for v in cmp.verdicts]
    out += _table(["Arm", "Paired tasks", *(CLAIM_TEXT[c].capitalize() for c in CLAIMS)], rows)
    out += [*reasons, ""]
    return out


def _arms(report: Report) -> list[str]:
    stats = report.stats
    out = ["## Arms", ""]
    out += _table([*_ARM_HEADER, "Frontier"], _arm_rows(stats.arms, report))
    out += [
        "Frontier: `$` = on the cost-quality frontier (no arm is cheaper per task and at least "
        "as good), `s` = on the latency-quality frontier (median seconds).",
        "",
        "### Efficiency and speed",
        "",
    ]
    rows = [
        [
            a.arm,
            f"{a.output_tokens_per_s:.0f}" if a.output_tokens_per_s is not None else "-",
            f"{a.tokens_per_solved:,.0f}" if a.tokens_per_solved is not None else "-",
            f"{a.quality_per_dollar:.1f}" if a.quality_per_dollar is not None else "-",
            f"{a.quality_per_minute:.3f}" if a.quality_per_minute is not None else "-",
            f"{a.calls_per_item:.1f}" if a.calls_per_item is not None else "-",
            str(a.errors),
        ]
        for a in stats.arms
    ]
    out += _table(
        [
            "Arm",
            "Out tok/s (effective)",
            "Tokens / solved",
            "Quality / $",
            "Quality / min",
            "Calls",
            "Errors",
        ],
        rows,
    )
    speed = [
        [a.arm, model, f"{rate:.0f}", f"{a.ttft_ms.get(model, float('nan')):.0f}"]
        for a in stats.arms
        for model, rate in a.decode_tokens_per_s.items()
    ]
    if speed:
        out += ["Streamed speed per model:", ""]
        out += _table(["Arm", "Model", "Decode tok/s", "TTFT ms"], speed)
    rows = [
        [
            a.arm,
            money(a.eval_cost_usd),
            money(a.eval_cost_per_item),
            seconds(a.eval_seconds_per_item),
        ]
        for a in stats.arms
    ]
    out += [
        "### Cost of measuring",
        "",
        "Scoring and judging cost and time, kept apart: never part of an arm's cost or latency.",
        "",
    ]
    out += _table(["Arm", "Eval $ total", "Eval $ / item", "Eval s / item"], rows)
    variance = [
        [a.arm, "-" if a.repeat_sd is None else f"{a.repeat_sd:.3f}", pct(a.flip_rate)]
        for a in stats.arms
    ]
    out += ["### Variance across repeats", ""]
    out += _table(
        ["Arm", "Mean sd of quality across repeats", "Tasks whose solved flips"], variance
    )
    return out


def _scope_reasons(comparisons: list[Comparison]) -> list[str]:
    """Why each per-category verdict is what it is (the all-tasks ones are under the headline)."""
    out: list[str] = []
    for cmp in comparisons:
        if cmp.scope == "all":
            continue
        if len({v.reason for v in cmp.verdicts}) == 1:
            out.append(f"- {cmp.scope} ({cmp.n_tasks} tasks): {cmp.verdicts[0].reason}.")
        else:
            out += [f"- {verdict_line(cmp, v)}" for v in cmp.verdicts]
    return [*out, ""] if out else []


def _comparisons(report: Report) -> list[str]:
    stats = report.stats
    out = ["## Comparisons against the baseline", ""]
    for arm in stats.arms:
        rows: list[list[str]] = []
        for cmp in (c for c in stats.comparisons if c.challenger == arm.arm):
            by_claim = {v.claim: v for v in cmp.verdicts}
            q = cmp.quality
            rows.append(
                [
                    cmp.scope,
                    str(cmp.n_tasks),
                    interval_text(q.difference, "+.3f"),
                    f"{q.wins}/{q.ties}/{q.losses}",
                    "-" if q.sign_test_p is None else f"{q.sign_test_p:.3f}",
                    "-" if q.effect_size is None else f"{q.effect_size:+.2f}",
                    interval_text(cmp.cost_per_solved_ratio, ".2f"),
                    interval_text(cmp.seconds_ratio, ".2f"),
                    *(OUTCOME_SYMBOL[by_claim[c].outcome] for c in CLAIMS),
                ]
            )
        if not rows:
            continue
        out += [f"### {arm.arm} vs {stats.baseline}", ""]
        out += _table(
            [
                "Scope",
                "Tasks",
                "Δ quality (95% CI)",
                "W/T/L",
                "Sign test p",
                "Effect d",
                "$/solved ratio",
                "Seconds ratio",
                "Cheaper",
                "Faster",
                "Not worse",
                "Better",
            ],
            rows,
        )
        out += _scope_reasons([c for c in stats.comparisons if c.challenger == arm.arm])
    out += [
        "Δ quality is the arm minus the baseline, paired by task. Ratios are the arm over the "
        "baseline (below 1 is better). W/T/L counts tasks the arm won, tied (within "
        f"{stats.tie:g}) or lost. Effect d is the mean paired difference over its spread across "
        "tasks.",
        "",
    ]
    return out


def _breakdowns(report: Report) -> list[str]:
    stats = report.stats
    out = ["## By category", ""]
    for category, arms in stats.by_category.items():
        out += [f"### {category}", ""]
        out += _table(_ARM_HEADER, _arm_rows(arms))
    if stats.by_difficulty:
        out += ["## By difficulty", ""]
        for level, arms in stats.by_difficulty.items():
            out += [f"### {level}", ""]
            out += _table(_ARM_HEADER, _arm_rows(arms))
    return out


def _side(side: ExampleSide) -> list[str]:
    quality = "-" if side.quality is None else f"{side.quality:.2f}"
    out = [f"- **{side.arm}** (repeat {side.repeat}): completion {quality}"]
    out += [
        f"  - gate {g['id']}: {({True: 'pass', False: 'FAIL', None: 'unverified'})[g['passed']]}"
        for g in side.gates
    ]
    out += [
        f"  - evidence {e.id or e.kind}: {e.kind} {e.status}: {e.summary}" for e in side.evidence
    ]
    out += [f"  - screenshot {name}: `{path}`" for name, path in side.screenshots.items()]
    if side.justification:
        out += [f"  - judge: {side.justification}"]
    return out


def _example(example: Example) -> list[str]:
    out = [
        f"### {example.task_id} ({example.category}): completion differs by {example.gap:+.2f}",
        "",
    ]
    out += _side(example.challenger) + _side(example.baseline) + [""]
    return out


def render_markdown(report: Report) -> str:
    run = report.run
    out = [f"# Benchmark report: {run.run_id}", ""]
    out += [f"> {note}" for note in report.notes]
    if report.notes:
        out.append("")
    out += _headline(report)
    out += _arms(report)
    out += _comparisons(report)
    out += _breakdowns(report)
    gate = report.judge_gate
    if gate is not None:
        out += ["## Judge reliability", ""]
        shown = ", ".join(
            f"{j} {'uncalibrated' if a is None else f'{a:.0%}'}" for j, a in gate.accuracy.items()
        )
        out += [f"Accuracy floor {gate.floor:.0%}: {shown}.", ""]
        out += [f"- {r}" for r in gate.reasons] + ([""] if gate.reasons else [])
    if report.examples:
        out += ["## Most different answers, with evidence", ""]
        for example in report.examples:
            out += _example(example)
    out += ["## Methodology", ""]
    out += [f"- **{label}:** {text}" for label, text in methodology(report)]
    out.append("")
    return "\n".join(out)
