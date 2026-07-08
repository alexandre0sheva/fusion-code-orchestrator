"""Shared formatting for cumulative Fusion statistics."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fusion.storage.run_store import FusionStats, ShadowComparisonRecord


def stats_to_dict(
    stats: FusionStats,
    recent_shadow: list[ShadowComparisonRecord] | None = None,
) -> dict[str, Any]:
    """Serialize stats plus derived metrics for MCP/CLI JSON output."""
    data = asdict(stats)
    data["shadow_win_rate_percent"] = stats.shadow_win_rate_percent
    data["estimated_savings_usd"] = stats.estimated_savings_usd
    data["estimated_savings_percent"] = stats.estimated_savings_percent
    if recent_shadow is not None:
        data["recent_shadow"] = [asdict(record) for record in recent_shadow]
    return data


def format_stats_markdown(
    stats: FusionStats,
    recent_shadow: list[ShadowComparisonRecord] | None = None,
) -> str:
    """Render the cumulative stats dashboard as markdown."""
    lines = ["## Fusion Stats", "", "### Runs"]
    lines.append(f"- Total runs: {stats.total_runs} ({stats.completed_runs} completed)")
    lines.append(f"- Average wall time: {stats.avg_latency_ms / 1000:.1f}s")
    lines.extend(["", "### Cost vs baseline"])
    lines.append(f"- Fusion total spend: ${stats.total_fusion_cost_usd:.4f}")
    if stats.baseline_estimate_runs:
        lines.append(
            f"- Baseline estimate for the same work: "
            f"${stats.total_baseline_estimated_cost_usd:.4f} "
            f"({stats.baseline_estimate_runs} runs with estimates)"
        )
        savings_pct = stats.estimated_savings_percent
        pct_text = f" ({savings_pct:.1f}%)" if savings_pct is not None else ""
        lines.append(f"- Estimated savings: ${stats.estimated_savings_usd:.4f}{pct_text}")
    else:
        lines.append("- Baseline estimate: no runs with baseline data yet")

    lines.extend(["", "### Shadow A/B vs baseline (real head-to-head)"])
    if stats.shadow_total:
        lines.append(
            f"- Judged comparisons: {stats.shadow_total} — "
            f"Fusion wins {stats.shadow_fusion_wins}, "
            f"baseline wins {stats.shadow_baseline_wins}, ties {stats.shadow_ties}"
        )
        win_rate = stats.shadow_win_rate_percent
        if win_rate is not None:
            lines.append(f"- Fusion win-rate (ties = 0.5): {win_rate:.1f}%")
        if (
            stats.shadow_avg_fusion_score is not None
            and stats.shadow_avg_baseline_score is not None
        ):
            lines.append(
                f"- Avg blind-judge score: Fusion {stats.shadow_avg_fusion_score:.2f} "
                f"vs baseline {stats.shadow_avg_baseline_score:.2f}"
            )
        lines.append(
            f"- Actual cost in shadowed runs: Fusion ${stats.shadow_fusion_cost_usd:.4f} "
            f"vs baseline ${stats.shadow_baseline_cost_usd:.4f}"
        )
        if (
            stats.shadow_avg_fusion_latency_ms is not None
            and stats.shadow_avg_baseline_latency_ms is not None
        ):
            lines.append(
                f"- Avg latency: Fusion {stats.shadow_avg_fusion_latency_ms / 1000:.1f}s "
                f"vs baseline {stats.shadow_avg_baseline_latency_ms / 1000:.1f}s"
            )
    else:
        lines.append(
            "- No shadow comparisons yet. Enable with FUSION_SHADOW_MODE=sampled|always "
            "or pass shadow_baseline=true on a tool call."
        )

    if stats.by_task_type:
        lines.extend(["", "### By task type"])
        for task in stats.by_task_type:
            lines.append(
                f"- {task.task_type}: {task.runs} runs, ${task.total_cost_usd:.4f}, "
                f"avg {task.avg_latency_ms / 1000:.1f}s"
            )

    if recent_shadow:
        lines.extend(["", "### Recent shadow comparisons"])
        for record in recent_shadow:
            scores = ""
            if record.fusion_score is not None and record.baseline_score is not None:
                scores = f" ({record.fusion_score:.2f} vs {record.baseline_score:.2f})"
            lines.append(
                f"- {record.created_at} · {record.task_type or '?'} · "
                f"winner: {record.winner}{scores} · run {record.run_id}"
            )
    return "\n".join(lines)
