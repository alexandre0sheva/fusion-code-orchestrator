"""Typer CLI for fusion-code-orchestrator."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from typer.core import TyperGroup

from fusion.cli.models_cmd import models_app
from fusion.config import paths as fusion_paths
from fusion.config.catalog import catalog_warnings, load_catalog
from fusion.config.env import load_env
from fusion.config.layers import (
    ConfigError,
    flatten,
    parse_scalar,
    resolve_config,
    set_cli_overrides,
)
from fusion.config.loader import load_baseline, load_routing_policies
from fusion.mcp_server.schemas import (
    CompareClaudeRunsInput,
    DebugErrorInput,
    DecideArchitectureInput,
    EvalAnswerInput,
    PlanFeatureInput,
    ReviewDiffInput,
)
from fusion.mcp_server.tools import FusionTools
from fusion.orchestration.pipelines import PipelineContext, Settings, build_pipeline
from fusion.orchestration.strategy import load_strategy_book
from fusion.routing.classifier import TaskType
from fusion.storage.run_store import RunStore
from fusion.telemetry.cost import PricingRegistry, UsageSummary, compare_to_baseline


class _FusionGroup(TyperGroup):
    """Show configuration problems as a message and exit code 1, not a traceback."""

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except ConfigError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None


app = typer.Typer(
    name="fusion",
    help="Fusion Code Orchestrator — multi-model coding workflow engine",
    no_args_is_help=True,
    cls=_FusionGroup,
)
runs_app = typer.Typer(help="Inspect orchestration run history")
config_app = typer.Typer(help="Validate Fusion configuration")
strategies_app = typer.Typer(help="Inspect strategies (the panels Fusion can run)")
app.add_typer(runs_app, name="runs")
app.add_typer(config_app, name="config")
app.add_typer(strategies_app, name="strategies")
app.add_typer(models_app, name="models")
console = Console()

load_env()


@app.callback()
def _main(
    overrides: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Override a config key for this run, e.g. --set fanout.max_concurrency=2",
        ),
    ] = None,
) -> None:
    parsed: dict[str, Any] = {}
    for item in overrides or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            msg = f"--set expects KEY=VALUE (for example fanout.max_concurrency=2), got {item!r}"
            raise typer.BadParameter(msg)
        parsed[key.strip()] = parse_scalar(value)
    set_cli_overrides(parsed)


def _ensure_mock(mock: bool) -> None:
    if mock:
        os.environ["FUSION_DEFAULT_PROVIDER"] = "mock"


def _tools(db_path: str | None, mock: bool) -> FusionTools:
    _ensure_mock(mock)
    return FusionTools(db_path=db_path, use_mock=mock)


def _run[T](tools: FusionTools, coro: Coroutine[Any, Any, T]) -> T:
    """Run one tool call, then close provider HTTP clients on the same event loop."""

    async def _go() -> T:
        try:
            return await coro
        finally:
            await tools.aclose()

    return asyncio.run(_go())


def _print_json(data: dict[str, object]) -> None:
    console.print_json(json.dumps(data, indent=2, default=str))


@app.command("stats")
def stats(
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print raw JSON")] = False,
    recent_shadow: Annotated[
        int, typer.Option(help="Recent shadow comparisons to show")
    ] = 10,
) -> None:
    """Show cumulative Fusion stats: spend vs baseline, savings, shadow win-rate."""
    from fusion.telemetry.stats_format import format_stats_markdown, stats_to_dict

    store = RunStore(db_path=db_path)
    fusion_stats = store.get_stats()
    recent = store.list_shadow_comparisons(limit=recent_shadow)
    if as_json:
        _print_json(stats_to_dict(fusion_stats, recent))
        return
    console.print(format_stats_markdown(fusion_stats, recent))


@app.command("review-diff")
def review_diff(
    file: Annotated[Path, typer.Option("--file", help="Path to diff file")],
    context: Annotated[str, typer.Option(help="Additional context")] = "",
    goals: Annotated[str, typer.Option(help="Review goals")] = "",
    strategy: Annotated[
        str | None, typer.Option(help="Strategy name (see `fusion strategies list`)")
    ] = None,
    mock: Annotated[bool, typer.Option(help="Use mock provider")] = False,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Run code review pipeline from a diff file."""
    diff_text = file.read_text(encoding="utf-8")
    tools = _tools(db_path, mock)
    result = _run(
        tools,
        tools.fusion_review_diff(
            ReviewDiffInput(diff=diff_text, context=context, goals=goals, strategy=strategy)
        )
    )
    _print_json(result)


@app.command()
def debug(
    error_file: Annotated[
        Path | None, typer.Option("--error-file", help="Path to error message file")
    ] = None,
    error: Annotated[str, typer.Option(help="Error message text")] = "",
    logs_file: Annotated[Path | None, typer.Option("--logs-file", help="Path to logs file")] = None,
    strategy: Annotated[
        str | None, typer.Option(help="Strategy name (see `fusion strategies list`)")
    ] = None,
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
    result = _run(
        tools,
        tools.fusion_debug_error(
            DebugErrorInput(error_message=error_message, logs=logs, strategy=strategy)
        )
    )
    _print_json(result)


@app.command()
def decide(
    question: Annotated[str, typer.Option(help="Architecture decision question")],
    constraints: Annotated[str, typer.Option(help="Constraints")] = "",
    strategy: Annotated[
        str | None, typer.Option(help="Strategy name (see `fusion strategies list`)")
    ] = None,
    mock: Annotated[bool, typer.Option(help="Use mock provider")] = False,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Run architecture decision pipeline."""
    tools = _tools(db_path, mock)
    result = _run(
        tools,
        tools.fusion_decide_architecture(
            DecideArchitectureInput(
                question=question, constraints=constraints, strategy=strategy
            )
        )
    )
    _print_json(result)


@app.command()
def plan(
    feature_file: Annotated[Path, typer.Option("--feature-file", help="Feature description file")],
    constraints: Annotated[str, typer.Option(help="Constraints")] = "",
    strategy: Annotated[
        str | None, typer.Option(help="Strategy name (see `fusion strategies list`)")
    ] = None,
    mock: Annotated[bool, typer.Option(help="Use mock provider")] = False,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Run implementation plan pipeline."""
    feature = feature_file.read_text(encoding="utf-8")
    tools = _tools(db_path, mock)
    result = _run(
        tools,
        tools.fusion_plan_feature(
            PlanFeatureInput(
                feature_description=feature, constraints=constraints, strategy=strategy
            )
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
    result = _run(
        tools,
        tools.fusion_eval_answer(EvalAnswerInput(question=question, answer=answer))
    )
    _print_json(result)


@app.command("compare-claude-runs")
def compare_claude_runs(
    task_file: Annotated[Path, typer.Option("--task-file", help="Original prompt/task file")],
    opus_file: Annotated[Path, typer.Option("--opus-file", help="Claude Code + Opus output")],
    fusion_file: Annotated[
        Path,
        typer.Option("--fusion-file", help="Claude Code + Fusion output"),
    ],
    context_file: Annotated[
        Path | None,
        typer.Option("--context-file", help="Shared verification/context file"),
    ] = None,
    opus_cost: Annotated[float | None, typer.Option(help="Measured Opus cost USD")] = None,
    fusion_cost: Annotated[float | None, typer.Option(help="Measured Fusion cost USD")] = None,
    opus_latency_ms: Annotated[int | None, typer.Option(help="Measured Opus latency ms")] = None,
    fusion_latency_ms: Annotated[
        int | None,
        typer.Option(help="Measured Fusion latency ms"),
    ] = None,
    mock: Annotated[bool, typer.Option(help="Use mock provider")] = False,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Compare Claude Code + Opus output against Claude Code + Fusion output."""
    tools = _tools(db_path, mock)
    result = _run(
        tools,
        tools.fusion_compare_claude_runs(
            CompareClaudeRunsInput(
                task_prompt=task_file.read_text(encoding="utf-8"),
                opus_output=opus_file.read_text(encoding="utf-8"),
                fusion_output=fusion_file.read_text(encoding="utf-8"),
                context=context_file.read_text(encoding="utf-8") if context_file else "",
                opus_cost_usd=opus_cost,
                fusion_cost_usd=fusion_cost,
                opus_latency_ms=opus_latency_ms,
                fusion_latency_ms=fusion_latency_ms,
            )
        )
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


@runs_app.command("costs")
def runs_costs(
    limit: Annotated[int, typer.Option(help="Max runs to include")] = 50,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Summarize recent run costs and latency."""
    store = RunStore(db_path=db_path)
    runs = store.list_runs(limit=limit)
    total_cost = sum(run.total_cost_usd for run in runs)
    total_latency = sum(run.total_latency_ms for run in runs)
    table = Table(title="Fusion Run Costs")
    table.add_column("Runs", justify="right")
    table.add_column("Total cost", justify="right")
    table.add_column("Avg cost", justify="right")
    table.add_column("Avg latency", justify="right")
    count = len(runs)
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
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Compare a stored Fusion run against the configured baseline model."""
    store = RunStore(db_path=db_path)
    record = store.get_run(run_id)
    if record is None:
        console.print(f"[red]Run not found: {run_id}[/red]")
        raise typer.Exit(1)
    output = record.output_data or {}
    comparison = output.get("cost_comparison")
    if comparison:
        _print_json(comparison)
        return
    usage = UsageSummary(
        total_input_tokens=sum(s.input_tokens for s in record.steps),
        total_output_tokens=sum(s.output_tokens for s in record.steps),
        total_tokens=sum(s.input_tokens + s.output_tokens for s in record.steps),
        fusion_wall_latency_ms=round(record.total_latency_ms),
    )
    _print_json(
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
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Export recent runs as JSONL."""
    if format != "jsonl":
        console.print("[red]Only --format jsonl is currently supported[/red]")
        raise typer.Exit(1)
    store = RunStore(db_path=db_path)
    for record in store.list_runs(limit=limit):
        console.print(
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


@app.command("init")
def init(
    force: Annotated[bool, typer.Option(help="Overwrite an existing user config")] = False,
    import_legacy: Annotated[
        bool,
        typer.Option(
            "--import-legacy",
            help="Copy a v0.1.0 ./fusion_runs.db into the user data directory",
        ),
    ] = False,
    legacy_path: Annotated[
        Path | None, typer.Option(help="Legacy database to import (default: ./fusion_runs.db)")
    ] = None,
) -> None:
    """Create a commented starter user config and show the next steps."""
    from fusion.storage.legacy import LegacyImportError, import_legacy_db

    target = fusion_paths.user_config_file()
    if target.exists() and not force:
        typer.echo(f"{target} already exists; leaving it alone (use --force to replace it).")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        template = Path(__file__).resolve().parent.parent / "config" / "starter_config.yaml"
        target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        typer.echo(f"Wrote starter config: {target}")

    if import_legacy:
        try:
            result = import_legacy_db(legacy_path or fusion_paths.legacy_db_path())
        except LegacyImportError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        plural = "" if result.runs == 1 else "s"
        typer.echo(
            f"Imported {result.runs} run{plural} into {result.destination}; "
            f"{result.source} was left in place."
        )

    typer.echo(
        "\nNext steps:\n"
        "  1. Set provider keys (environment or a .env file): ANTHROPIC_API_KEY, "
        "OPENAI_API_KEY, GOOGLE_API_KEY\n"
        "  2. See the models and prices Fusion will use:  fusion models list\n"
        "  3. Check the effective configuration:  fusion config show --resolved\n"
        f"Runs are stored in {fusion_paths.resolve_db_path()}"
    )


@config_app.command("paths")
def config_paths() -> None:
    """Show where Fusion reads configuration and keeps its database."""
    for label, path in (
        ("user config", fusion_paths.user_config_file()),
        ("project config", fusion_paths.project_config_file()),
        ("database", fusion_paths.resolve_db_path()),
        ("legacy database", fusion_paths.legacy_db_path()),
    ):
        state = "exists" if path.exists() else "missing"
        typer.echo(f"{label:<16} {path}  ({state})")


@config_app.command("show")
def config_show(
    resolved: Annotated[
        bool, typer.Option("--resolved", help="Print every merged value with its origin")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print raw JSON")] = False,
    key_filter: Annotated[
        str | None, typer.Option("--filter", help="Only keys containing this text")
    ] = None,
) -> None:
    """Show the configuration layers, or with --resolved the effective values and origins."""
    config = resolve_config()
    if not resolved:
        loaded = {layer.path for layer in config.layers if layer.path is not None}
        for layer in config.layers:
            keys = ", ".join(sorted(layer.data)) or "(nothing set)"
            typer.echo(f"{layer.name}: {keys}")
        for label, file in (
            ("user", fusion_paths.user_config_file()),
            ("project", fusion_paths.project_config_file()),
        ):
            if file not in loaded:
                typer.echo(f"{label}:{file}: (not found)")
        typer.echo("\nUse --resolved to see every value and which layer set it.")
        return
    rows = [
        {"key": key, "value": value, "origin": config.origin_of(key) or "packaged"}
        for key, value in flatten(config.data)
        if key_filter is None or key_filter in key
    ]
    if as_json:
        typer.echo(json.dumps(rows, indent=2, default=str))
        return
    table = Table(title="Resolved configuration")
    table.add_column("Key")
    table.add_column("Value")
    table.add_column("Origin")
    for row in rows:
        table.add_row(str(row["key"]), str(row["value"]), str(row["origin"]))
    console.print(table)


@strategies_app.command("list")
def strategies_list(
    as_json: Annotated[bool, typer.Option("--json", help="Print raw JSON")] = False,
) -> None:
    """List strategies, what each runs, and which budget levels map to them."""
    book = load_strategy_book()
    budgets_for: dict[str, list[str]] = {}
    for budget, name in book.budget_map.items():
        budgets_for.setdefault(name, []).append(budget)
    if as_json:
        rows = [
            {**book.get(name).model_dump(), "budgets": budgets_for.get(name, [])}
            for name in book.names()
        ]
        typer.echo(json.dumps(rows, indent=2, default=str))
        return
    table = Table(title="Strategies")
    table.add_column("Name", no_wrap=True)
    for column in ("Kind", "Models", "Rounds", "Aggregator", "Judge", "Budget alias"):
        table.add_column(column, overflow="fold")
    for name in book.names():
        strategy = book.get(name)
        aggregator = strategy.aggregator_model or strategy.aggregator
        table.add_row(
            name,
            strategy.kind,
            ", ".join(m.model for m in strategy.members),
            str(strategy.rounds),
            "-" if strategy.kind == "solo" else aggregator,
            strategy.judge,
            ", ".join(budgets_for.get(name, [])),
        )
    console.print(table)


@config_app.command("validate")
def config_validate(
    strict: Annotated[bool, typer.Option(help="Fail on missing provider env vars")] = False,
) -> None:
    """Validate YAML config, pricing, baseline, fanout, and provider env vars."""
    issues: list[str] = []
    warnings: list[str] = []
    catalog = load_catalog()
    models = catalog.models
    routing = load_routing_policies()
    baselines = load_baseline(catalog=catalog).baselines

    for alias, model in models.items():
        if not model.provider:
            issues.append(f"Model {alias} has no provider")
        if model.enabled and model.price_at() is None:
            warnings.append(f"No price in effect today for enabled model {alias}")
    warnings.extend(catalog_warnings(catalog))

    for entry in baselines:
        if entry.enabled and entry.model and entry.model not in models:
            issues.append(f"Baseline {entry.name} references unknown model {entry.model}")

    referenced = {policy.judge_model for policy in routing.policies.values()}
    strategies = load_strategy_book()
    referenced.update(strategies.referenced_models())
    for alias in sorted(referenced - set(models)):
        issues.append(f"Routing policy or strategy references unknown model {alias}")

    fanout = routing.fanout
    if fanout.global_timeout_seconds < fanout.per_model_timeout_seconds:
        warnings.append("fanout.global_timeout_seconds is below per-model timeout")

    provider_env = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "google": "GOOGLE_API_KEY",
    }
    for provider, env_name in provider_env.items():
        enabled = any(m.enabled and m.provider == provider for m in models.values())
        if enabled and not os.environ.get(env_name):
            message = f"{env_name} missing for enabled {provider} models"
            (issues if strict else warnings).append(message)

    if issues:
        for issue in issues:
            console.print(f"[red]ERROR[/red] {issue}")
        raise typer.Exit(1)
    for warning in warnings:
        console.print(f"[yellow]WARN[/yellow] {warning}")
    console.print("[green]Configuration valid[/green]")


@app.command("compare-cost")
def compare_cost(
    fusion_run_id: Annotated[str, typer.Option(help="Fusion run_id from MCP output")],
    opus_input_tokens: Annotated[int, typer.Option(help="Opus input tokens from Claude Code")],
    opus_output_tokens: Annotated[int, typer.Option(help="Opus output tokens from Claude Code")],
    opus_model: Annotated[str, typer.Option(help="Catalog model alias for pricing")] = (
        "claude-opus"
    ),
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Compare Fusion run cost vs Claude Opus token usage."""
    from fusion.storage.run_store import RunStore
    from fusion.telemetry.cost import estimate_alias_cost

    store = RunStore(db_path=db_path)
    record = store.get_run(fusion_run_id)
    if record is None:
        console.print(f"[red]Run not found: {fusion_run_id}[/red]")
        raise typer.Exit(1)

    fusion_in = sum(s.input_tokens for s in record.steps)
    fusion_out = sum(s.output_tokens for s in record.steps)
    try:
        estimate = estimate_alias_cost(
            opus_model,
            input_tokens=opus_input_tokens,
            output_tokens=opus_output_tokens,
        )
    except KeyError:
        console.print(f"[red]Unknown catalog model: {opus_model}[/red]")
        raise typer.Exit(1) from None
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
    strategy: Annotated[
        str | None, typer.Option(help="Strategy name (see `fusion strategies list`)")
    ] = None,
    db_path: Annotated[str | None, typer.Option(help="SQLite database path")] = None,
) -> None:
    """Run end-to-end mock pipeline."""
    try:
        task_type = TaskType(task)
    except ValueError:
        console.print(f"[red]Unknown task type: {task}[/red]")
        raise typer.Exit(1) from None

    _ensure_mock(True)
    pipeline = build_pipeline(Settings(use_mock=True, db_path=db_path))
    ctx = PipelineContext(task_type=task_type, primary_content=content, strategy=strategy)
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
    review_diff(file=diff, context=context, goals="", strategy=None, mock=False, db_path=db_path)


@app.command()
def version() -> None:
    """Show version."""
    from fusion import __version__

    console.print(f"fusion-code-orchestrator v{__version__}")


if __name__ == "__main__":
    app()
