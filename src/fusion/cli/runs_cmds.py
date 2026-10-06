"""``fusion runs ...``: inspect the stored run history."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.table import Table

from fusion.cli.common import (
    CliError,
    DbPathOption,
    JsonOption,
    console,
    deprecated,
    echo_json,
)
from fusion.storage.run_store import RunRecord, RunStore
from fusion.telemetry.cost import PricingRegistry, UsageSummary, compare_to_baseline

runs_app = typer.Typer(help="Inspect orchestration run history")


def _store(db_path: str | None) -> RunStore:
    return RunStore(db_path=db_path)


def _get_run(store: RunStore, run_id: str) -> RunRecord:
    record = store.get_run(run_id)
    if record is None:
        msg = f"Run not found: {run_id}"
        raise CliError(msg, hint="List recent runs with `fusion runs list`.")
    return record


@runs_app.command("list")
def runs_list(
    limit: Annotated[int, typer.Option(help="Max runs to show")] = 10,
    as_json: JsonOption = False,
    db_path: DbPathOption = None,
) -> None:
    """List recent orchestration runs."""
    runs = _store(db_path).list_runs(limit=limit)
    if as_json:
        echo_json(
            [
                {
                    "run_id": run.run_id,
                    "task_type": run.task_type,
                    "status": run.status,
                    "cost_usd": run.total_cost_usd,
                    "latency_ms": run.total_latency_ms,
                }
                for run in runs
            ]
        )
        return
    table = Table(title="Recent Runs")
    table.add_column("Run ID")
    table.add_column("Task")
    table.add_column("Status")
    table.add_column("Cost")
    table.add_column("Latency")
    for run in runs:
        table.add_row(
            run.run_id,
            run.task_type,
            run.status,
            f"${run.total_cost_usd:.4f}",
            f"{run.total_latency_ms:.0f}ms",
        )
    console.print(table)


@runs_app.command("show")
def runs_show(
    run_id: Annotated[str, typer.Argument(help="Run ID to display")],
    db_path: DbPathOption = None,
) -> None:
    """Show details for a specific run (JSON)."""
    record = _get_run(_store(db_path), run_id)
    echo_json(
        {
            "run_id": record.run_id,
            "task_type": record.task_type,
            "status": record.status,
            "input": record.sanitized_input,
            "routing": record.routing,
            "output": record.output_data,
            "warnings": record.warnings,
            "cost_usd": record.total_cost_usd,
            "latency_ms": record.total_latency_ms,
            "input_tokens": sum(s.input_tokens for s in record.steps),
            "output_tokens": sum(s.output_tokens for s in record.steps),
            "steps": [
                {
                    "step": s.step_name,
                    "model": s.model_name,
                    "cost_usd": s.cost_usd,
                    "latency_ms": s.latency_ms,
                }
                for s in record.steps
            ],
        }
    )


@runs_app.command("costs")
def runs_costs(
    limit: Annotated[int, typer.Option(help="Max runs to include")] = 50,
    as_json: JsonOption = False,
    db_path: DbPathOption = None,
) -> None:
    """Summarize recent run costs and latency."""
    runs = _store(db_path).list_runs(limit=limit)
    total_cost = sum(run.total_cost_usd for run in runs)
    total_latency = sum(run.total_latency_ms for run in runs)
    count = len(runs)
    if as_json:
        echo_json(
            {
                "runs": count,
                "total_cost_usd": total_cost,
                "avg_cost_usd": total_cost / count if count else 0.0,
                "avg_latency_ms": total_latency / count if count else 0.0,
            }
        )
        return
    table = Table(title="Fusion Run Costs")
    table.add_column("Runs", justify="right")
    table.add_column("Total cost", justify="right")
    table.add_column("Avg cost", justify="right")
    table.add_column("Avg latency", justify="right")
    table.add_row(
        str(count),
        f"${total_cost:.4f}",
        f"${(total_cost / count):.4f}" if count else "$0.0000",
        f"{(total_latency / count):.0f}ms" if count else "0ms",
    )
    console.print(table)


@runs_app.command("compare-baseline")
def runs_compare_baseline(
    run_id: Annotated[str, typer.Argument(help="Run ID to compare")],
    db_path: DbPathOption = None,
) -> None:
    """Compare a stored Fusion run against the configured baseline model (JSON)."""
    record = _get_run(_store(db_path), run_id)
    comparison = (record.output_data or {}).get("cost_comparison")
    if comparison:
        echo_json(comparison)
        return
    usage = UsageSummary(
        total_input_tokens=sum(s.input_tokens for s in record.steps),
        total_output_tokens=sum(s.output_tokens for s in record.steps),
        total_tokens=sum(s.input_tokens + s.output_tokens for s in record.steps),
        fusion_wall_latency_ms=round(record.total_latency_ms),
    )
    echo_json(
        compare_to_baseline(
            usage=usage,
            fusion_total_cost_usd=record.total_cost_usd,
            fusion_cost_known=True,
            pricing=PricingRegistry(),
        ).model_dump()
    )


@runs_app.command("export")
def runs_export(
    format: Annotated[str, typer.Option("--format", help="Output format: jsonl")] = "jsonl",
    limit: Annotated[int, typer.Option(help="Max runs to export")] = 100,
    db_path: DbPathOption = None,
) -> None:
    """Export recent runs as JSONL (one JSON object per line)."""
    if format != "jsonl":
        msg = "Only --format jsonl is currently supported"
        raise typer.BadParameter(msg, param_hint="--format")
    for record in _store(db_path).list_runs(limit=limit):
        typer.echo(
            json.dumps(
                {
                    "run_id": record.run_id,
                    "task_type": record.task_type,
                    "status": record.status,
                    "sanitized_input": record.sanitized_input,
                    "routing": record.routing,
                    "output": record.output_data,
                    "warnings": record.warnings,
                    "total_cost_usd": record.total_cost_usd,
                    "total_latency_ms": record.total_latency_ms,
                },
                default=str,
            )
        )


# -- deprecated, one release ----------------------------------------------------------------------


def list_runs(
    limit: Annotated[int, typer.Option(help="Max runs to show")] = 10,
    db_path: DbPathOption = None,
) -> None:
    """Deprecated: use `fusion runs list`."""
    deprecated("list-runs", "runs list")
    runs_list(limit=limit, db_path=db_path)


def compare_cost(
    fusion_run_id: Annotated[str, typer.Option(help="Fusion run_id from MCP output")],
    opus_input_tokens: Annotated[int, typer.Option(help="Opus input tokens from Claude Code")],
    opus_output_tokens: Annotated[int, typer.Option(help="Opus output tokens from Claude Code")],
    opus_model: Annotated[str, typer.Option(help="Catalog model alias for pricing")] = (
        "claude-opus"
    ),
    db_path: DbPathOption = None,
) -> None:
    """Deprecated: use `fusion runs compare-baseline RUN_ID` or `fusion compare-claude-runs`."""
    from fusion.telemetry.cost import estimate_alias_cost

    deprecated("compare-cost", "runs compare-baseline RUN_ID")
    record = _get_run(_store(db_path), fusion_run_id)
    fusion_in = sum(s.input_tokens for s in record.steps)
    fusion_out = sum(s.output_tokens for s in record.steps)
    try:
        estimate = estimate_alias_cost(
            opus_model, input_tokens=opus_input_tokens, output_tokens=opus_output_tokens
        )
    except KeyError:
        msg = f"Unknown catalog model: {opus_model}"
        raise CliError(msg) from None
    opus_cost = estimate.amount_usd or 0.0

    table = Table(title="Fusion vs Opus cost (estimated list prices)")
    table.add_column("Source")
    table.add_column("Input tokens", justify="right")
    table.add_column("Output tokens", justify="right")
    table.add_column("Cost USD", justify="right")
    table.add_column("Latency", justify="right")
    table.add_row(
        "Fusion MCP",
        f"{fusion_in:,}",
        f"{fusion_out:,}",
        f"${record.total_cost_usd:.4f}",
        f"{record.total_latency_ms:.0f}ms",
    )
    table.add_row(
        "Claude Opus",
        f"{opus_input_tokens:,}",
        f"{opus_output_tokens:,}",
        f"${opus_cost:.4f}",
        "n/a",
    )
    console.print(table)

    delta = record.total_cost_usd - opus_cost
    if delta < 0:
        console.print(f"[green]Fusion cheaper by ${abs(delta):.4f}[/green]")
    elif delta > 0:
        console.print(f"[yellow]Fusion costlier by ${delta:.4f}[/yellow]")
    else:
        console.print("Estimated costs match.")
    console.print(
        "[dim]Opus tokens are from Claude Code, not Fusion MCP. "
        "Judge LLM calls may add uncaptured Fusion cost.[/dim]"
    )


def register_legacy(app: typer.Typer) -> None:
    app.command("list-runs")(list_runs)
    app.command("compare-cost")(compare_cost)
