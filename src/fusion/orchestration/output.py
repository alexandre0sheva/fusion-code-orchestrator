"""Turn a finished run into the outputs callers see: display text, usage, steps, persisted JSON."""

from __future__ import annotations

from typing import Any

from fusion.evals.engine import EvalEngine
from fusion.evals.schemas import (
    ContextEvalResult,
    FinalEvalResult,
    HybridEvalResult,
    ModelResponseEval,
    OutcomeEvalResult,
)
from fusion.orchestration.claims import cluster_line, top_clusters
from fusion.orchestration.ledger import CallRecord
from fusion.orchestration.result import PipelineResult, build_usage_summary
from fusion.orchestration.schemas import CostLatencyInfo, Detail, PipelineEvals, StepUsage
from fusion.storage.run_store import RunStepRecord, RunStore
from fusion.telemetry.cost import (
    CostComparison,
    PricingRegistry,
    UsageSummary,
    compare_to_baseline,
)


def _format_cost(amount: float | None, known: bool) -> str:
    if amount is None or not known:
        return "unknown"
    return f"${amount:.4f} estimated"


_HEADLINE_KEYS = (
    "answer",
    "summary",
    "recommended_option",
    "minimal_fix_strategy",
    "safer_answer",
)


def _headline(result: PipelineResult) -> str:
    """The answer in words: a structured headline field, else the whole final answer."""
    for key in _HEADLINE_KEYS:
        value = result.structured_output.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return result.final_answer.strip()


def _confidence_line(result: PipelineResult) -> str:
    confidence = f"{result.final_eval.confidence:.2f}"
    report = result.agreement
    if report is None:
        return confidence
    if report.low_information:
        return (
            f"{confidence}, low information: {report.n_models} model answered, "
            "so agreement could not be measured"
        )
    return (
        f"{confidence} (agreement {report.score:.2f} across {report.n_models} of "
        f"{report.n_requested} models, evidence {report.evidence_rate:.0%})"
    )


def step_name(record: CallRecord) -> str:
    return "synthesis" if record.stage == "synthesis" else f"{record.stage}:{record.model_alias}"


class ResultPresenter:
    """Builds every user-facing view of a ``PipelineResult``."""

    def __init__(
        self, *, eval_engine: EvalEngine, run_store: RunStore, pricing: PricingRegistry
    ) -> None:
        self._eval_engine = eval_engine
        self._run_store = run_store
        self._pricing = pricing
        self._footers: dict[str, str | None] = {}

    # -- evals ----------------------------------------------------------------------------

    def build_evals(
        self,
        context_eval: ContextEvalResult,
        evaluations: list[ModelResponseEval],
        disagreement: dict[str, Any],
        final_eval: FinalEvalResult,
        judge_quality: HybridEvalResult | None,
        warnings: list[str],
    ) -> PipelineEvals:
        raw = self._eval_engine.build_pipeline_evals(
            context=context_eval,
            per_answer=evaluations,
            disagreement=disagreement,
            final=final_eval,
            judge_quality=judge_quality,
            outcome=OutcomeEvalResult(),
            warnings=warnings,
        )
        return PipelineEvals(**raw)

    # -- persisted steps -------------------------------------------------------------------

    def step_records(self, result: PipelineResult) -> list[RunStepRecord]:
        """One stored step per LLM call (panel steps keep their per-answer evals)."""
        steps: list[RunStepRecord] = []
        panel_evals = {pr.model_name: pr for pr in result.panel_results}
        for record in self._records(result):
            panel = panel_evals.get(record.model_alias) if record.stage == "panel" else None
            eval_data: dict[str, Any] = {"cost_known": record.cost_known}
            if panel is not None:
                eval_data = {
                    **panel.evaluation.model_dump(),
                    "cost_known": panel.cost_known,
                    "cost_is_estimate": panel.cost_is_estimate,
                }
            if not record.ok:
                eval_data.update(success=False, error=record.error, error_type=record.error_type)
            steps.append(
                RunStepRecord(
                    step_name=step_name(record),
                    model_name=record.model_alias,
                    provider=record.provider,
                    input_tokens=record.input_tokens or 0,
                    output_tokens=record.output_tokens or 0,
                    cost_usd=record.cost_usd or 0.0,
                    latency_ms=record.latency_ms,
                    eval_data=eval_data,
                )
            )
        steps.append(
            RunStepRecord(step_name="final_eval", eval_data=result.final_eval.model_dump())
        )
        return steps

    @staticmethod
    def _records(result: PipelineResult) -> list[CallRecord]:
        if result.ledger is None:
            return []
        return result.ledger.ordered_records(result.trace.panel_models)

    # -- cost / usage views ---------------------------------------------------------------

    def cost_latency(self, result: PipelineResult) -> CostLatencyInfo:
        """Token/cost breakdown for MCP and CLI consumers, itemizing every call."""
        steps = [
            StepUsage(
                step_name=step_name(r),
                model_name=r.model_alias,
                provider=r.provider,
                input_tokens=r.input_tokens or 0,
                output_tokens=r.output_tokens or 0,
                cost_usd=r.cost_usd,
                cost_known=r.cost_known,
                cost_is_estimate=r.cost_is_estimate,
                latency_ms=r.latency_ms,
            )
            for r in self._records(result)
            if r.ok
        ]
        cost_known = result.ledger.total_cost().known if result.ledger else True
        usage = result.usage
        warnings = [
            *result.warnings,
            "Claude Code / Opus usage is billed separately and is not visible to Fusion MCP.",
        ]
        return CostLatencyInfo(
            total_cost_usd=result.total_cost_usd if cost_known else None,
            total_cost_known=cost_known,
            total_latency_ms=result.total_latency_ms,
            total_input_tokens=(usage.total_input_tokens if usage else 0) or 0,
            total_output_tokens=(usage.total_output_tokens if usage else 0) or 0,
            steps=steps,
            warnings=warnings,
        )

    def usage_for(self, result: PipelineResult) -> UsageSummary:
        if result.usage is not None:
            return result.usage
        if result.ledger is not None:
            return build_usage_summary(
                result.ledger,
                model_order=result.trace.panel_models,
                wall_latency_ms=result.total_latency_ms,
                fanout=result.fanout,
            )
        return UsageSummary(fusion_wall_latency_ms=round(result.total_latency_ms))

    def comparison_for(self, result: PipelineResult, usage: UsageSummary) -> CostComparison:
        return result.cost_comparison or compare_to_baseline(
            usage=usage,
            fusion_total_cost_usd=result.total_cost_usd,
            fusion_cost_known=False,
            pricing=self._pricing,
        )

    def common_output_fields(
        self, result: PipelineResult, title: str, detail: Detail = "compact"
    ) -> dict[str, Any]:
        usage = self.usage_for(result)
        cost_comparison = self.comparison_for(result, usage)
        return {
            "display_markdown": self.display_markdown(
                title=title,
                result=result,
                usage=usage,
                cost_comparison=cost_comparison,
                detail=detail,
            ),
            "result": result.structured_output,
            "claims": result.claims,
            "agreement": result.agreement,
            "usage": usage,
            "cost_comparison": cost_comparison,
            "warnings": result.warnings,
        }

    # -- display ---------------------------------------------------------------------------

    def display_markdown(
        self,
        *,
        title: str,
        result: PipelineResult,
        usage: UsageSummary,
        cost_comparison: CostComparison,
        detail: Detail = "compact",
    ) -> str:
        """Compact: the answer, the top claims, confidence and one cost line. Full: everything."""
        if detail == "full":
            return self._full_markdown(title, result, usage, cost_comparison)
        lines = [f"## {title}", "", "### Answer", _headline(result)]
        lines.extend(self._claim_lines(result, "Key claims", limit=5))
        lines.extend(["", "### Confidence", _confidence_line(result)])
        lines.extend(["", "### Cost", self._compact_cost(result, usage, cost_comparison)])
        if result.warnings:
            lines.extend(["", "### Caveats"])
            lines.extend(f"- {warning}" for warning in result.warnings[:3])
            if len(result.warnings) > 3:
                lines.append(f"- {len(result.warnings) - 3} more in `warnings` (detail: full)")
        return "\n".join(lines)

    def _full_markdown(
        self,
        title: str,
        result: PipelineResult,
        usage: UsageSummary,
        cost_comparison: CostComparison,
    ) -> str:
        lines = [f"## {title}", "", "### Recommendation", result.final_answer.strip()]
        lines.extend(self._claim_lines(result, "Claims", limit=1000))
        lines.extend(["", "### Confidence", _confidence_line(result)])
        lines.extend(["", "### Cost & usage"])
        lines.extend(self._cost_lines(result, usage, cost_comparison))
        lines.extend(self._shadow_lines(result))
        if cost_comparison.comparison_notes:
            lines.append(f"- Note: {cost_comparison.comparison_notes[0]}")
        footer = None
        if result.mode_settings.lifetime_stats:
            footer = self.lifetime_footer(result.run_id)
        if footer:
            lines.append(footer)
        if result.warnings:
            lines.extend(["", "### Caveats"])
            lines.extend(f"- {warning}" for warning in result.warnings)
        return "\n".join(lines)

    @staticmethod
    def _claim_lines(result: PipelineResult, heading: str, *, limit: int) -> list[str]:
        shown = top_clusters(result.claims, limit)
        if not shown:
            return []
        n = result.agreement.n_models if result.agreement else 1
        lines = ["", f"### {heading}"]
        for cluster in shown:
            lines.append(f"- {cluster_line(cluster)} ({cluster.support}/{n}, {cluster.status})")
        if len(result.claims) > len(shown):
            lines.append(f"- {len(result.claims) - len(shown)} more (detail: full)")
        return lines

    @staticmethod
    def _compact_cost(
        result: PipelineResult, usage: UsageSummary, cost_comparison: CostComparison
    ) -> str:
        parts = [
            _format_cost(cost_comparison.fusion_total_cost_usd, cost_comparison.fusion_cost_known)
        ]
        if cost_comparison.savings_percent is not None and cost_comparison.fusion_is_cheaper:
            parts.append(
                f"{cost_comparison.savings_percent:.0f}% below the "
                f"{cost_comparison.baseline_name} baseline estimate"
            )
        parts.append(f"{usage.fusion_wall_latency_ms / 1000:.1f}s")
        calls = usage.successful_model_calls
        parts.append(f"{result.routing.strategy}, {calls} call{'' if calls == 1 else 's'}")
        return " · ".join(parts)

    @staticmethod
    def _cost_lines(
        result: PipelineResult, usage: UsageSummary, cost_comparison: CostComparison
    ) -> list[str]:
        fusion_cost = _format_cost(
            cost_comparison.fusion_total_cost_usd, cost_comparison.fusion_cost_known
        )
        baseline_cost = _format_cost(
            cost_comparison.baseline_estimated_cost_usd, cost_comparison.baseline_cost_known
        )
        lines = [
            f"- Strategy: {result.routing.strategy}",
            f"- Fusion cost: {fusion_cost}",
            f"- {cost_comparison.baseline_name} baseline estimate: {baseline_cost}",
        ]
        if cost_comparison.savings_usd is not None:
            label = "savings" if cost_comparison.fusion_is_cheaper else "extra cost"
            percent = (
                f" / {abs(cost_comparison.savings_percent):.1f}%"
                if cost_comparison.savings_percent is not None
                else ""
            )
            lines.append(f"- Estimated {label}: ${abs(cost_comparison.savings_usd):.4f}{percent}")
        else:
            lines.append("- Estimated savings: unknown")
        panel_count = len(result.fanout.calls) if result.fanout else len(result.panel_results)
        succeeded = result.fanout.success_count if result.fanout else len(result.panel_results)
        failed = result.fanout.failed_count if result.fanout else 0
        lines.append(f"- Fusion wall time: {usage.fusion_wall_latency_ms / 1000:.1f}s")
        lines.append(f"- Panel: {panel_count} models, {succeeded} succeeded, {failed} failed")
        if result.refinement and result.refinement.ran:
            lines.append(
                f"- Refinement: {result.refinement.refined_count}/"
                f"{len(result.refinement.calls)} answers refined"
            )
        return lines

    @staticmethod
    def _shadow_lines(result: PipelineResult) -> list[str]:
        shadow = result.shadow
        if not (shadow and shadow.ran):
            return []
        lines: list[str] = []
        if shadow.winner in {"fusion", "baseline", "tie"}:
            verdict = {
                "fusion": "Fusion won",
                "baseline": f"{shadow.baseline_name} won",
                "tie": "tie",
            }[shadow.winner]
            scores = ""
            if shadow.fusion_score is not None and shadow.baseline_score is not None:
                scores = (
                    f" (blind judge: fusion {shadow.fusion_score:.2f} vs "
                    f"baseline {shadow.baseline_score:.2f})"
                )
            lines.append(f"- Shadow A/B vs {shadow.baseline_name}: {verdict}{scores}")
        if shadow.baseline_latency_ms is not None:
            lines.append(
                f"- Shadow baseline latency: {shadow.baseline_latency_ms / 1000:.1f}s actual"
            )
        return lines

    def lifetime_footer(self, run_id: str) -> str | None:
        """One-line cumulative summary appended to every run's display output.

        The display text is built twice per run (returned and persisted); the stats query runs
        once and the footer is reused for the same run.
        """
        if run_id in self._footers:
            return self._footers[run_id]
        footer = self._compute_footer()
        if len(self._footers) >= 64:
            self._footers.pop(next(iter(self._footers)))
        self._footers[run_id] = footer
        return footer

    def _compute_footer(self) -> str | None:
        try:
            stats = self._run_store.get_stats()
        except Exception:  # noqa: BLE001 — stats must never break a run
            return None
        if stats.total_runs < 2:
            return None
        parts = [
            f"Lifetime: {stats.total_runs} runs",
            f"${stats.total_fusion_cost_usd:.2f} spent",
        ]
        if stats.baseline_estimate_runs:
            savings_pct = stats.estimated_savings_percent
            pct_text = f" ({savings_pct:.1f}% saved)" if savings_pct is not None else ""
            parts.append(
                f"vs ${stats.total_baseline_estimated_cost_usd:.2f} baseline est.{pct_text}"
            )
        win_rate = stats.shadow_win_rate_percent
        if win_rate is not None:
            parts.append(f"shadow win-rate {win_rate:.0f}% (n={stats.shadow_total})")
        return "- " + " · ".join(parts)

    # -- persistence -----------------------------------------------------------------------

    def persisted_output(self, result: PipelineResult) -> dict[str, Any]:
        usage = self.usage_for(result)
        return {
            "final_answer": result.final_answer,
            "structured_output": result.structured_output,
            "context_eval": result.context_eval.model_dump(),
            "panel_results": [
                {"model": p.model_name, "content": p.content, "eval": p.evaluation.model_dump()}
                for p in result.panel_results
            ],
            "final_eval": result.final_eval.model_dump(),
            "disagreement": result.disagreement,
            "routing": result.routing.model_dump(),
            "mode": result.mode.value,
            "evals": result.evals.model_dump() if result.evals else {},
            "usage": result.usage.model_dump() if result.usage else {},
            "cost_comparison": (
                result.cost_comparison.model_dump() if result.cost_comparison else {}
            ),
            "fanout": result.fanout.model_dump() if result.fanout else {},
            "refinement": result.refinement.model_dump() if result.refinement else {},
            "shadow": (
                result.shadow.model_dump(exclude={"baseline_answer"}) if result.shadow else {}
            ),
            "ledger": result.ledger.summary() if result.ledger else {},
            "task_metrics": result.task_metrics.model_dump() if result.task_metrics else {},
            "display_markdown": self.display_markdown(
                title="Fusion Result",
                result=result,
                usage=usage,
                cost_comparison=self.comparison_for(result, usage),
                detail="full",
            ),
            "claims": [c.model_dump() for c in result.claims],
            "agreement": result.agreement.model_dump() if result.agreement else {},
            "cascade": result.cascade.model_dump() if result.cascade else {},
            "budget": result.budget.model_dump() if result.budget else {},
            "warnings": result.warnings,
        }
