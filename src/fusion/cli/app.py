"""Typer CLI for fusion-code-orchestrator."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from fusion.config.env import load_env
from fusion.mcp_server.schemas import (
    DebugErrorInput,
    DecideArchitectureInput,
    EvalAnswerInput,
    PlanFeatureInput,
    ReviewDiffInput,
)
from fusion.mcp_server.tools import FusionTools
from fusion.orchestration.pipelines import PipelineContext, create_pipeline
from fusion.routing.classifier import TaskType
from fusion.storage.run_store import RunStore

app = typer.Typer(
    name="fusion",
    help="Fusion Code Orchestrator — multi-model coding workflow engine",
    no_args_is_help=True,
)
runs_app = typer.Typer(help="Inspect orchestration run history")
app.add_typer(runs_app, name="runs")
console = Console()

load_env()


def _ensure_mock(mock: bool) -> None:
    if mock:
        os.environ["FUSION_DEFAULT_PROVIDER"] = "mock"


def _tools(db_path: str | None, mock: bool) -> FusionTools:
    _ensure_mock(mock)
    return FusionTools(db_path=db_path, use_mock=mock)


def _print_json(data: dict[str, object]) -> None:
    console.print_json(json.dumps(data, indent=2, default=str))


@app.command("review-diff")
def review_diff(
    file: Annotated[Path, typer.Option("--file", help="Path to diff file")],
    context: Annotated[str, typer.Option(help="Additional context")] = "",
    goals: Annotated[str, typer.Option(help="Review goals")] = "",
    mock: Annotated[bool, typer.Option(help="Use mock provider")] = False,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Run code review pipeline from a diff file."""
    diff_text = file.read_text(encoding="utf-8")
    tools = _tools(db_path, mock)
    result = asyncio.run(
        tools.fusion_review_diff(
            ReviewDiffInput(diff=diff_text, context=context, goals=goals)
        )
    )
    _print_json(result)


@app.command()
def debug(
    error_file: Annotated[
        Path | None, typer.Option("--error-file", help="Path to error message file")
    ] = None,
    error: Annotated[str, typer.Option(help="Error message text")] = "",
    logs_file: Annotated[Path | None, typer.Option("--logs-file", help="Path to logs file")] = "",
    mock: Annotated[bool, typer.Option(help="Use mock provider")] = False,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Run debug pipeline from CLI."""
    error_message = error
    if error_file:
        error_message = error_file.read_text(encoding="utf-8")
    if not error_message:
        console.print("[red]Provide --error-file or --error[/red]")
        raise typer.Exit(1)
    logs = logs_file.read_text(encoding="utf-8") if logs_file else ""
    tools = _tools(db_path, mock)
    result = asyncio.run(
        tools.fusion_debug_error(DebugErrorInput(error_message=error_message, logs=logs))
    )
    _print_json(result)


@app.command()
def decide(
    question: Annotated[str, typer.Option(help="Architecture decision question")],
    constraints: Annotated[str, typer.Option(help="Constraints")] = "",
    mock: Annotated[bool, typer.Option(help="Use mock provider")] = False,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Run architecture decision pipeline."""
    tools = _tools(db_path, mock)
    result = asyncio.run(
        tools.fusion_decide_architecture(
            DecideArchitectureInput(question=question, constraints=constraints)
        )
    )
    _print_json(result)


@app.command()
def plan(
    feature_file: Annotated[Path, typer.Option("--feature-file", help="Feature description file")],
    constraints: Annotated[str, typer.Option(help="Constraints")] = "",
    mock: Annotated[bool, typer.Option(help="Use mock provider")] = False,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Run implementation plan pipeline."""
    feature = feature_file.read_text(encoding="utf-8")
    tools = _tools(db_path, mock)
    result = asyncio.run(
        tools.fusion_plan_feature(
            PlanFeatureInput(feature_description=feature, constraints=constraints)
        )
    )
    _print_json(result)


@app.command("eval-answer")
def eval_answer(
    question_file: Annotated[Path, typer.Option("--question-file", help="Question file")],
    answer_file: Annotated[Path, typer.Option("--answer-file", help="Answer file")],
    mock: Annotated[bool, typer.Option(help="Use mock provider")] = False,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Run answer evaluation pipeline."""
    question = question_file.read_text(encoding="utf-8")
    answer = answer_file.read_text(encoding="utf-8")
    tools = _tools(db_path, mock)
    result = asyncio.run(
        tools.fusion_eval_answer(EvalAnswerInput(question=question, answer=answer))
    )
    _print_json(result)


@runs_app.command("list")
def runs_list(
    limit: Annotated[int, typer.Option(help="Max runs to show")] = 10,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """List recent orchestration runs."""
    store = RunStore(db_path=db_path)
    runs = store.list_runs(limit=limit)

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
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Show details for a specific run."""
    store = RunStore(db_path=db_path)
    record = store.get_run(run_id)
    if record is None:
        console.print(f"[red]Run not found: {run_id}[/red]")
        raise typer.Exit(1)
    _print_json(
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


@app.command("compare-cost")
def compare_cost(
    fusion_run_id: Annotated[str, typer.Option(help="Fusion run_id from MCP output")],
    opus_input_tokens: Annotated[int, typer.Option(help="Opus input tokens from Claude Code")],
    opus_output_tokens: Annotated[int, typer.Option(help="Opus output tokens from Claude Code")],
    opus_model: Annotated[str, typer.Option(help="Opus model id for pricing")] = (
        "claude-opus-4-8"
    ),
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Compare Fusion run cost vs Claude Opus token usage."""
    from fusion.storage.run_store import RunStore
    from fusion.telemetry.pricing import estimate_cost_usd

    store = RunStore(db_path=db_path)
    record = store.get_run(fusion_run_id)
    if record is None:
        console.print(f"[red]Run not found: {fusion_run_id}[/red]")
        raise typer.Exit(1)

    fusion_in = sum(s.input_tokens for s in record.steps)
    fusion_out = sum(s.output_tokens for s in record.steps)
    opus_cost = estimate_cost_usd(
        input_tokens=opus_input_tokens,
        output_tokens=opus_output_tokens,
        model=opus_model,
    )

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


@app.command("compare-implement")
def compare_implement_cmd(
    task: Annotated[str, typer.Option(help="Implementation task")],
    workspace: Annotated[str, typer.Option(help="Workspace root path")] = "",
    constraints: Annotated[str, typer.Option(help="Constraints")] = "",
    verify_command: Annotated[str, typer.Option(help="Verification command")] = "",
    max_steps: Annotated[int, typer.Option(help="Max agent steps per arm")] = 40,
    mock: Annotated[bool, typer.Option(help="Use mock providers")] = False,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Run Opus vs Fusion implementation benchmark."""
    import os
    from pathlib import Path

    from fusion.benchmark.compare import compare_implementations

    if mock:
        os.environ["FUSION_DEFAULT_PROVIDER"] = "mock"
    root = workspace or os.environ.get("FUSION_WORKSPACE_ROOT", os.getcwd())
    result = asyncio.run(
        compare_implementations(
            task=task,
            workspace_root=Path(root),
            constraints=constraints,
            verify_command=verify_command,
            max_agent_steps=max_steps,
            db_path=db_path,
            use_mock=mock,
        )
    )
    _print_json(result.model_dump())


@app.command()
def mcp(
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Start the MCP server (stdio transport)."""
    import sys

    from fusion.mcp_server.server import run_server

    if sys.stdin.isatty():
        err = Console(stderr=True)
        err.print(
            "[yellow]Fusion MCP uses stdin/stdout for JSON-RPC — not an interactive shell.[/yellow]"
        )
        err.print(
            "[dim]Add this server in Cursor or Claude Code MCP settings; "
            "do not press Enter here.[/dim]"
        )
        err.print(
            "[dim]To smoke-test providers: uv run python evals/runners/compare_pipelines.py[/dim]"
        )

    run_server(db_path=db_path)


@app.command("run-mock")
def run_mock(
    task: Annotated[str, typer.Option(help="Task type")] = "code_review",
    content: Annotated[
        str, typer.Option(help="Primary content")
    ] = "Sample diff content for testing",
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Run end-to-end mock pipeline."""
    try:
        task_type = TaskType(task)
    except ValueError:
        console.print(f"[red]Unknown task type: {task}[/red]")
        raise typer.Exit(1) from None

    _ensure_mock(True)
    pipeline = create_pipeline(db_path=db_path)
    ctx = PipelineContext(task_type=task_type, primary_content=content)
    result = asyncio.run(pipeline.run(ctx))

    console.print(f"\n[bold green]Run ID:[/bold green] {result.run_id}")
    summary = str(result.structured_output.get("summary", result.final_answer))[:500]
    console.print(f"[bold]Summary:[/bold]\n{summary}")
    console.print(
        f"\nCost: ${result.total_cost_usd:.4f} | Latency: {result.total_latency_ms:.0f}ms"
    )


@app.command("list-runs")
def list_runs(
    limit: Annotated[int, typer.Option(help="Max runs to show")] = 10,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """List recent orchestration runs (legacy alias)."""
    runs_list(limit=limit, db_path=db_path)


@app.command()
def review(
    diff: Annotated[Path, typer.Argument(help="Path to diff file")],
    context: Annotated[str, typer.Option(help="Additional context")] = "",
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Run code review pipeline (legacy alias)."""
    review_diff(file=diff, context=context, mock=False, db_path=db_path)


@app.command()
def version() -> None:
    """Show version."""
    from fusion import __version__

    console.print(f"fusion-code-orchestrator v{__version__}")


if __name__ == "__main__":
    app()
