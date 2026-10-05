"""``fusion bench``: plan, run, resume and inspect studies."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from fusion.bench.arms import arm_book, parse_arms
from fusion.bench.plan import BenchPlan, make_plan
from fusion.bench.runner import BenchEnv, BenchProgress, BenchRun, build_env, run_bench
from fusion.bench.spec import (
    BenchConfig,
    BenchTask,
    load_dataset,
    resolve_dataset,
    select_tasks,
)
from fusion.bench.spend import default_ledger
from fusion.bench.store import BenchItem, BenchStore
from fusion.bench.summary import summarize
from fusion.bench.virtual import run_virtual
from fusion.config.layers import ConfigError
from fusion.config.paths import bench_results_dir
from fusion.providers.base import close_providers
from fusion.routing.policy import RoutingPolicy

bench_app = typer.Typer(
    help="Measure whether a panel of cheap models beats one big model (ground-truth studies)",
    no_args_is_help=True,
)
console = Console()
err = Console(stderr=True)

_Dataset = Annotated[str, typer.Option("--dataset", "-d", help="Dataset file, directory or name")]
_Arms = Annotated[
    str | None,
    typer.Option(help="Strategies, comma-separated (name or name=strategy); 'default' = six arms"),
]
_Repeats = Annotated[int | None, typer.Option(help="Runs of each (task, arm); default 3")]
_MaxUsd = Annotated[
    float | None,
    typer.Option("--max-usd", help="Spending limit for the run (required unless --mock)"),
]
_Mock = Annotated[
    bool, typer.Option("--mock", help="Simulated models: free, deterministic, no API keys")
]
_Seed = Annotated[int | None, typer.Option(help="Base sampling seed; repeat n uses seed + n - 1")]
_Concurrency = Annotated[int | None, typer.Option(help="Jobs at once; default 8")]
_Limit = Annotated[
    int | None, typer.Option(help="Use only this many tasks (spread over categories)")
]
_Config = Annotated[
    Path | None,
    typer.Option("--config", help="YAML file with BenchConfig fields; options override it"),
]


def _build_config(
    *,
    dataset: str | None,
    arms: str | None,
    repeats: int | None,
    max_usd: float | None,
    seed: int | None,
    concurrency: int | None,
    mock: bool,
    no_cache: bool = False,
    redact: bool = False,
    limit: int | None = None,
    judge_models: str | None = None,
    config_file: Path | None = None,
) -> BenchConfig:
    data: dict[str, Any] = {}
    if config_file is not None:
        loaded = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            msg = f"{config_file} must hold a mapping of BenchConfig fields"
            raise ConfigError(msg)
        data.update(loaded)
    if dataset is not None:
        data["dataset"] = dataset
    if arms is not None:
        data["arms"] = [a.model_dump() for a in parse_arms(arms)]
    for key, value in (
        ("repeats", repeats),
        ("max_usd", max_usd),
        ("seed", seed),
        ("concurrency", concurrency),
        ("limit", limit),
    ):
        if value is not None:
            data[key] = value
    if mock:
        data["mock"] = True
    if no_cache:
        data["cache"] = False
    if redact:
        data["redact"] = True
    if judge_models:
        data["judge_models"] = [m.strip() for m in judge_models.split(",") if m.strip()]
    missing = [k for k in ("dataset", "arms") if k not in data]
    if missing:
        msg = f"Missing {' and '.join('--' + k for k in missing)} (or put it in --config)"
        raise ConfigError(msg)
    if "max_usd" not in data:
        if not data.get("mock"):
            msg = "--max-usd is required for a live run: say how much this run may spend"
            raise ConfigError(msg)
        data["max_usd"] = 100.0  # simulated money: the limit only matters for exercising caps
    data["dataset"] = resolve_dataset(str(data["dataset"]))
    return BenchConfig.model_validate(data)


def _plan_for(cfg: BenchConfig, env: BenchEnv, tasks: list[BenchTask]) -> BenchPlan:
    book = arm_book(env.book, cfg.arms)
    routing = RoutingPolicy(env.routing_config, registry=env.registry, strategies=book)
    return make_plan(
        cfg,
        tasks,
        {a.name: book.get(a.name) for a in cfg.arms},
        routing=routing,
        registry=env.registry,
        pricing=env.pricing,
        spend_left_usd=None if cfg.mock or env.spend is None else env.spend.remaining(),
    )


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def _print_plan(plan: BenchPlan, cfg: BenchConfig) -> None:
    table = Table(
        title=f"Plan: {plan.tasks} tasks x {len(plan.arms)} arms x {plan.repeats} repeats"
    )
    table.add_column("Arm")
    for column in ("Jobs", "Expected", "Low", "High"):
        table.add_column(column, justify="right")
    for arm in plan.per_arm:
        table.add_row(
            arm.arm,
            str(arm.jobs),
            f"${arm.expected_usd:.4f}",
            f"${arm.low_usd:.4f}",
            f"${arm.high_usd:.4f}",
        )
    table.add_row(
        "[bold]Total[/bold]",
        str(plan.jobs),
        f"[bold]${plan.expected_usd:.4f}[/bold]",
        f"${plan.low_usd:.4f}",
        f"${plan.high_usd:.4f}",
    )
    console.print(table)
    where = "simulated, spends nothing" if not plan.live else f"budget ${plan.budget_usd:.2f}"
    console.print(
        f"Estimated time about {_duration(plan.wall_seconds)} at {cfg.concurrency} jobs at once; "
        f"{where}."
    )
    if plan.unpriced:
        console.print(f"[red]No catalog price for: {', '.join(plan.unpriced)}[/red]")
    if plan.fits:
        console.print("[green]The plan fits.[/green]")
        return
    console.print("[red]The plan does not fit the budget.[/red]")
    if plan.suggestion is not None:
        s = plan.suggestion
        limit = f" --limit {s.limit}" if s.limit else ""
        console.print(
            f"Largest design that fits ({s.note}), about ${s.expected_usd:.4f}:\n"
            f"  fusion bench run --dataset {cfg.dataset} --arms {','.join(s.arms)} "
            f"--repeats {s.repeats}{limit} --max-usd {cfg.max_usd:g}"
        )
    else:
        console.print("Not even one task on one arm fits; raise --max-usd.")


def _load(cfg: BenchConfig, *, providers: bool = True) -> tuple[list[BenchTask], BenchEnv]:
    tasks = select_tasks(load_dataset(cfg.dataset), cfg.limit, cfg.seed)
    try:
        return tasks, build_env(cfg, tasks, providers=providers)
    except RuntimeError as exc:  # no provider is configured
        raise ConfigError(str(exc)) from exc


def _execute(cfg: BenchConfig, env: BenchEnv, run_id: str | None) -> BenchRun:
    """Run on the right loop with a progress bar."""
    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=err,
        transient=True,
    )
    bar = progress.add_task("benchmark", total=None)

    def on_item(item: BenchItem, state: BenchProgress) -> None:
        progress.update(
            bar,
            total=state.total,
            completed=state.done,
            description=f"benchmark ${state.spent_usd:.4f}",
        )

    async def study() -> BenchRun:
        try:
            return await run_bench(cfg, env=env, run_id=run_id, on_item=on_item)
        finally:
            await close_providers(env.providers)  # HTTP clients, on the loop that opened them

    with progress:
        return run_virtual(study()) if cfg.mock else asyncio.run(study())


def _report(run: BenchRun) -> None:
    console.print(
        f"\n[bold]Run {run.run_id}[/bold]: {run.status}, {run.done_jobs}/{run.total_jobs} jobs "
        f"finished ({run.failed_jobs} errors), spent ${run.spent_usd:.4f}"
        + (f" + ${run.eval_spent_usd:.4f} scoring" if run.eval_spent_usd else "")
    )
    if run.stop_reason:
        console.print(f"[yellow]Stopped: {run.stop_reason}[/yellow]")
        console.print(f"Continue with: fusion bench resume {run.run_id} --max-usd <more>")
    if run.failed_jobs:
        console.print(f"Errors are tried again by: fusion bench resume {run.run_id}")
    _print_arms(run.items)
    console.print(f"Details: fusion bench show {run.run_id}")


def _print_arms(items: list[BenchItem]) -> None:
    table = Table(title="Arms")
    table.add_column("Arm", no_wrap=True)
    for column in ("N", "Solved", "Quality", "$/task", "$/solved", "p50 s", "p90 s", "Calls"):
        table.add_column(column, justify="right")

    def fmt(value: float | None, spec: str) -> str:
        return "-" if value is None else format(value, spec)

    for row in summarize(items):
        table.add_row(
            row.arm,
            str(row.items),
            fmt(None if row.solved_rate is None else row.solved_rate * 100, ".0f") + "%",
            fmt(row.mean_quality, ".2f"),
            fmt(row.mean_cost_usd, ".4f"),
            fmt(row.cost_per_solved_usd, ".4f"),
            fmt(row.seconds_p50, ".1f"),
            fmt(row.seconds_p90, ".1f"),
            fmt(row.mean_calls, ".1f"),
        )
    console.print(table)


# ---------------------------------------------------------------------------------- commands


@bench_app.command("plan")
def plan_cmd(
    dataset: _Dataset = "",
    arms: _Arms = None,
    repeats: _Repeats = None,
    max_usd: _MaxUsd = None,
    mock: _Mock = False,
    seed: _Seed = None,
    concurrency: _Concurrency = None,
    limit: _Limit = None,
    config: _Config = None,
) -> None:
    """Estimate cost and time of a study (no model is called) and fit it to --max-usd."""
    cfg = _build_config(
        dataset=dataset or None,
        arms=arms,
        repeats=repeats,
        max_usd=max_usd,
        seed=seed,
        concurrency=concurrency,
        mock=mock,
        limit=limit,
        config_file=config,
    )
    tasks, env = _load(cfg, providers=False)
    plan = _plan_for(cfg, env, tasks)
    _print_plan(plan, cfg)
    if not plan.fits:
        raise typer.Exit(2)


@bench_app.command("run")
def run_cmd(
    dataset: _Dataset = "",
    arms: _Arms = None,
    repeats: _Repeats = None,
    max_usd: _MaxUsd = None,
    mock: _Mock = False,
    seed: _Seed = None,
    concurrency: _Concurrency = None,
    limit: _Limit = None,
    config: _Config = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Do not reuse or store responses")
    ] = False,
    redact: Annotated[bool, typer.Option("--redact", help="Keep secret redaction on")] = False,
    judge_models: Annotated[
        str | None, typer.Option(help="Catalog aliases for LLM scorers")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Run even if the plan does not fit")] = False,
    run_id: Annotated[str | None, typer.Option(help="Name the run (default: generated)")] = None,
) -> None:
    """Run a study. Refuses to start unless `fusion bench plan` passes, unless --yes."""
    cfg = _build_config(
        dataset=dataset or None,
        arms=arms,
        repeats=repeats,
        max_usd=max_usd,
        seed=seed,
        concurrency=concurrency,
        mock=mock,
        no_cache=no_cache,
        redact=redact,
        limit=limit,
        judge_models=judge_models,
        config_file=config,
    )
    tasks, env = _load(cfg)
    plan = _plan_for(cfg, env, tasks)
    _print_plan(plan, cfg)
    if not plan.fits and not yes:
        err.print("Not starting: the plan does not pass. Adjust it, or pass --yes to run anyway.")
        raise typer.Exit(2)
    try:
        run = _execute(cfg, env, run_id)
    except KeyboardInterrupt:
        err.print("Interrupted. Finished jobs are saved; continue with `fusion bench resume`.")
        raise typer.Exit(130) from None
    _report(run)


@bench_app.command("resume")
def resume_cmd(
    run_id: Annotated[str, typer.Argument(help="Run to continue (see `fusion bench list`)")],
    max_usd: _MaxUsd = None,
    concurrency: _Concurrency = None,
) -> None:
    """Continue a stopped or interrupted run: finished jobs are skipped, errors are retried."""
    store = BenchStore(bench_results_dir())
    try:
        cfg = store.load_config(run_id)
    except FileNotFoundError as exc:
        raise ConfigError(str(exc)) from exc
    update: dict[str, Any] = {}
    if max_usd is not None:
        update["max_usd"] = max_usd
    if concurrency is not None:
        update["concurrency"] = concurrency
    cfg = cfg.model_copy(update=update)
    _, env = _load(cfg)
    try:
        run = _execute(cfg, env, run_id)
    except KeyboardInterrupt:
        err.print("Interrupted. Finished jobs are saved; continue with `fusion bench resume`.")
        raise typer.Exit(130) from None
    _report(run)


@bench_app.command("list")
def list_cmd(limit: Annotated[int, typer.Option(help="Runs to show")] = 20) -> None:
    """List benchmark runs, newest first."""
    runs = BenchStore(bench_results_dir()).list_runs(limit)
    if not runs:
        console.print(
            "No benchmark runs yet. Try: fusion bench run --dataset toy --arms default --mock"
        )
        return
    table = Table(title="Benchmark runs")
    table.add_column("Run", no_wrap=True)
    for column in ("Status", "Mode", "Jobs", "Spent"):
        table.add_column(column, no_wrap=True)
    table.add_column("Dataset", overflow="fold")
    for r in runs:
        table.add_row(
            r.run_id,
            r.status,
            "mock" if r.mock else "live",
            f"{r.done_jobs}/{r.total_jobs}",
            f"${r.spent_usd + r.eval_spent_usd:.4f}",
            Path(r.dataset).name,
        )
    console.print(table)


@bench_app.command("show")
def show_cmd(
    run_id: Annotated[str, typer.Argument(help="Run to show")],
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
) -> None:
    """Show a run: its state and one summary row per arm."""
    store = BenchStore(bench_results_dir())
    record = store.get_run(run_id)
    if record is None:
        known = ", ".join(r.run_id for r in store.list_runs(5)) or "none yet"
        msg = f"No benchmark run '{run_id}' (recent runs: {known})"
        raise ConfigError(msg)
    items = store.items(run_id)
    if as_json:
        payload = {
            "run": json.loads(record.model_dump_json()),
            "arms": [row.model_dump() for row in summarize(items)],
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    console.print(
        f"[bold]{record.run_id}[/bold]: {record.status}, {record.done_jobs}/{record.total_jobs} "
        f"jobs, spent ${record.spent_usd:.4f}"
        + (f" + ${record.eval_spent_usd:.4f} scoring" if record.eval_spent_usd else "")
        + (" (simulated)" if record.mock else "")
    )
    if record.stop_reason:
        console.print(f"Stopped: {record.stop_reason}")
    _print_arms(items)


@bench_app.command("spend")
def spend_cmd() -> None:
    """Show the live-spend ledger: what has been spent against the roadmap's cap."""
    ledger = default_ledger()
    entries = ledger.entries()
    console.print(
        f"Spent ${ledger.total():.4f} of ${ledger.cap_usd:.2f} "
        f"(${ledger.remaining():.4f} left), {len(entries)} entries in {ledger.path}"
    )


__all__ = ["bench_app"]
