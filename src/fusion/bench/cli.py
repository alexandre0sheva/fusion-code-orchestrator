"""``fusion bench``: plan, run, resume and inspect studies."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from fusion.bench.arms import arm_book, parse_arms
from fusion.bench.calibrate import default_mock_judges, run_artifact_calibration, run_calibration
from fusion.bench.datasets.build import (
    AUTHORING_DIR,
    CATEGORY_FILES,
    AuthoringError,
    compile_dir,
    render_jsonl,
    write_compiled,
)
from fusion.bench.datasets.generate import Generated, run_generation
from fusion.bench.datasets.validate import Rules, validate_dataset
from fusion.bench.meta import RunMeta, load_meta, reconstruct_meta
from fusion.bench.plan import BenchPlan, make_plan
from fusion.bench.recommend import recommend
from fusion.bench.report import Report, build_report, render_html, render_markdown
from fusion.bench.report.compare import compare_runs, render_comparison
from fusion.bench.report.html import cost_quality_svg
from fusion.bench.report.results import render_results
from fusion.bench.runner import BenchEnv, BenchProgress, BenchRun, build_env, run_bench
from fusion.bench.scoring import ScoringError
from fusion.bench.scoring.artifact_calibration import artifact_cases
from fusion.bench.scoring.calibration import (
    CalibrationReport,
    JudgeGate,
    build_cases,
    cases_from_file,
    judge_gate,
    load_reports,
    save_report,
)
from fusion.bench.spec import (
    BenchConfig,
    BenchTask,
    load_dataset,
    resolve_dataset,
    select_tasks,
)
from fusion.bench.spend import SPEND_FILE, SpendCapError, SpendLedger, default_ledger
from fusion.bench.stats import DEFAULT_BOOTSTRAP, DEFAULT_MARGIN, DEFAULT_MIN_TASKS, study_stats
from fusion.bench.stats import Rules as StatRules
from fusion.bench.store import BenchItem, BenchRunRecord, BenchStore
from fusion.bench.suite import list_suites, load_suite
from fusion.bench.summary import summarize
from fusion.bench.virtual import run_virtual
from fusion.config.layers import ConfigError
from fusion.config.paths import bench_results_dir
from fusion.providers.base import close_providers
from fusion.routing.model_registry import ModelRegistry
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
_Split = Annotated[
    str | None,
    typer.Option(
        help="dev (default), test (held out for the final study) or all; tasks without a split "
        "are always used"
    ),
]
_Suite = Annotated[
    str | None,
    typer.Option("--suite", help="A packaged suite (see `bench suite list`) or a suite YAML file"),
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
    split: str | None = None,
    config_file: Path | None = None,
    suite: str | None = None,
) -> BenchConfig:
    data: dict[str, Any] = {}
    if suite is not None:
        data.update(load_suite(suite).config)
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
    if split is not None:
        data["split"] = split
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


def _spend_left(cfg: BenchConfig, env: BenchEnv) -> float | None:
    """Live money the study may still spend: the ledger's remainder, less any reserve it keeps."""
    if cfg.mock or env.spend is None:
        return None
    left = env.spend.remaining()
    if cfg.spend_stop_usd is not None:
        left = min(left, max(cfg.spend_stop_usd - env.spend.total(), 0.0))
    return left


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
        spend_left_usd=_spend_left(cfg, env),
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
    tasks = select_tasks(load_dataset(cfg.dataset, cfg.split), cfg.limit, cfg.seed, cfg.quota)
    try:
        return tasks, build_env(cfg, tasks, providers=providers)
    except RuntimeError as exc:  # no provider is configured
        raise ConfigError(str(exc)) from exc


def _execute(
    cfg: BenchConfig, env: BenchEnv, run_id: str | None, *, retry_halted: bool = False
) -> BenchRun:
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
            return await run_bench(
                cfg, env=env, run_id=run_id, on_item=on_item, retry_halted=retry_halted
            )
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
    _print_gate(_gate_for(run.items, run.config.judge_models, run.config.mock))
    console.print(f"Details: fusion bench show {run.run_id}")


def _print_arms(items: list[BenchItem]) -> None:
    measured = any(i.metrics.eval_seconds > 0 or i.metrics.eval_cost_usd > 0 for i in items)
    table = Table(title="Arms")
    table.add_column("Arm", no_wrap=True)
    columns = ["N", "Solved", "Quality", "$/task", "$/solved", "p50 s", "p90 s", "Calls"]
    if measured:  # what measuring and judging cost, next to what the arm cost, never added to it
        columns += ["Eval $", "Eval s"]
    for column in columns:
        table.add_column(column, justify="right")

    def fmt(value: float | None, spec: str) -> str:
        return "-" if value is None else format(value, spec)

    for row in summarize(items):
        cells = [
            row.arm,
            str(row.items),
            fmt(None if row.solved_rate is None else row.solved_rate * 100, ".0f") + "%",
            fmt(row.mean_quality, ".2f"),
            fmt(row.mean_cost_usd, ".4f"),
            fmt(row.cost_per_solved_usd, ".4f"),
            fmt(row.seconds_p50, ".1f"),
            fmt(row.seconds_p90, ".1f"),
            fmt(row.mean_calls, ".1f"),
        ]
        if measured:
            cells += [fmt(row.mean_eval_cost_usd, ".4f"), fmt(row.mean_eval_seconds, ".1f")]
        table.add_row(*cells)
    console.print(table)


def _gate_for(items: list[BenchItem], judges: list[str], mock: bool) -> JudgeGate | None:
    """The judge-reliability gate of a run, or None when no judge scored an artifact in it."""
    judged = any(
        i.category in ("frontend", "performance") and i.score is not None and i.score.trail
        for i in items
    )
    if not judges or not judged:
        return None
    reports = [r for r in load_reports(bench_results_dir()) if r.mock == mock]
    return judge_gate(judges, reports)


def _print_gate(gate: JudgeGate | None) -> None:
    if gate is None:
        return
    if gate.blocked:
        console.print(
            "[bold red]Headline verdict blocked:[/bold red] "
            "the judges are not shown to be reliable."
        )
        for reason in gate.reasons:
            console.print(f"  - {reason}")
        console.print(
            "Calibrate them with: fusion bench calibrate-judge --artifacts --dataset v1 "
            "--judge-models <judges>"
        )
        return
    shown = ", ".join(f"{j} {a:.0%}" for j, a in gate.accuracy.items() if a is not None)
    console.print(f"Judge reliability: {shown} (floor {gate.floor:.0%}); headline verdict allowed.")


def _print_evidence(items: list[BenchItem], task_id: str) -> None:
    """Every item of one task: the gates, the criteria, the evidence and the judge's reasoning."""
    chosen = [i for i in items if i.task_id == task_id]
    if not chosen:
        known = ", ".join(sorted({i.task_id for i in items})[:8])
        msg = f"No items for task '{task_id}' in this run (tasks: {known} ...)"
        raise ConfigError(msg)
    for item in chosen:
        score = item.score
        console.print(
            f"\n[bold]{item.task_id}[/bold] arm {item.arm} repeat {item.repeat}: "
            + (f"completion {score.quality:.2f}" if score else "not scored")
            + (
                f"; eval {item.metrics.eval_seconds:.1f}s, ${item.metrics.eval_cost_usd:.4f} "
                "(not part of the arm's cost or time)"
            )
        )
        if score is None:
            continue
        for gate in score.details.get("gates", []):
            state = {True: "pass", False: "FAIL", None: "unverified"}[gate["passed"]]
            extra = f": {gate['detail']}" if gate.get("detail") else ""
            console.print(f"  gate {gate['id']}: {state}{extra}", markup=False)
        for crit in score.details.get("criteria", []):
            value = "-" if crit["score"] is None else f"{crit['score']:.2f}"
            console.print(
                f"  criterion {crit['id']} (w {crit['weight']:g}): {value} [{crit['basis']}]",
                markup=False,
            )
        for line in score.details.get("evidence", []):
            console.print(f"  {line}", markup=False)
        judge = score.details.get("judge")
        if judge:
            console.print(f"  judge: {judge.get('justification', '')}", markup=False)
            if judge.get("disagreement"):
                console.print(
                    f"  judges disagreed on: {', '.join(judge['disagreement'])}", markup=False
                )
        if score.trail:
            console.print(
                f"  trail: {len(score.trail)} judge tool calls (see results.jsonl)", markup=False
            )


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
    split: _Split = None,
    config: _Config = None,
    suite: _Suite = None,
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
        split=split,
        config_file=config,
        suite=suite,
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
    split: _Split = None,
    config: _Config = None,
    suite: _Suite = None,
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
        split=split,
        config_file=config,
        suite=suite,
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
    retry_halted: Annotated[
        bool,
        typer.Option(
            "--retry-halted",
            help="Also run again the jobs that halted (no quorum, a timed-out call); the earlier "
            "attempt stays in results.jsonl and its money in the run's total",
        ),
    ] = False,
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
        run = _execute(cfg, env, run_id, retry_halted=retry_halted)
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
    task: Annotated[
        str | None,
        typer.Option("--task", help="Show the evidence, gates and judge reasoning of one task"),
    ] = None,
) -> None:
    """Show a run: its state and one summary row per arm."""
    store = BenchStore(bench_results_dir())
    record = store.get_run(run_id)
    if record is None:
        known = ", ".join(r.run_id for r in store.list_runs(5)) or "none yet"
        msg = f"No benchmark run '{run_id}' (recent runs: {known})"
        raise ConfigError(msg)
    items = store.items(run_id)
    gate = _gate_for(items, record.config.judge_models, record.mock)
    if as_json:
        payload = {
            "run": json.loads(record.model_dump_json()),
            "arms": [row.model_dump() for row in summarize(items)],
            "judge_gate": json.loads(gate.model_dump_json()) if gate else None,
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
    _print_gate(gate)
    if task:
        _print_evidence(items, task)


def _run_inputs(
    store: BenchStore, run_id: str
) -> tuple[BenchRunRecord, list[BenchItem], RunMeta | None]:
    """A run, its items and the facts of its start (read from today's files when it has none)."""
    record = store.get_run(run_id)
    if record is None:
        known = ", ".join(r.run_id for r in store.list_runs(5)) or "none yet"
        msg = f"No benchmark run '{run_id}' (recent runs: {known})"
        raise ConfigError(msg)
    items = store.items(run_id)
    if not items:
        msg = f"Run '{run_id}' has no finished items yet: nothing to report"
        raise ConfigError(msg)
    meta = load_meta(store.run_dir(run_id))
    if meta is None:
        meta = reconstruct_meta(record.config, ModelRegistry.for_mode(use_mock=False).models)
    return record, items, meta


_Format = Annotated[str, typer.Option("--format", "-f", help="md (default), html or json")]
_Margin = Annotated[
    float,
    typer.Option(help="Quality an arm may lose and still count as 'not worse' (non-inferiority)"),
]
_MinTasks = Annotated[int, typer.Option(help="Paired tasks needed before any verdict is given")]
_Resamples = Annotated[int, typer.Option(help="Bootstrap resamples per interval")]


@bench_app.command("report")
def report_cmd(
    run_id: Annotated[str, typer.Argument(help="Run to report on")],
    format: _Format = "md",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write here (html defaults to the run's folder)"),
    ] = None,
    baseline: Annotated[
        str | None,
        typer.Option(help="Arm the others are compared with (default: solo-frontier or the first)"),
    ] = None,
    margin: _Margin = DEFAULT_MARGIN,
    min_tasks: _MinTasks = DEFAULT_MIN_TASKS,
    resamples: _Resamples = DEFAULT_BOOTSTRAP,
) -> None:
    """Statistics and verdicts for a run: confidence intervals, paired comparisons, $ per solved
    task, the cost and speed frontiers, and what the evidence supports (md, one HTML file, json)."""
    if format not in ("md", "html", "json"):
        msg = f"--format must be md, html or json, not '{format}'"
        raise ConfigError(msg)
    store = BenchStore(bench_results_dir())
    record, items, meta = _run_inputs(store, run_id)
    spend: float | None = None
    if not record.mock:
        try:
            spend = SpendLedger(store.root / SPEND_FILE).total()
        except SpendCapError:
            spend = None
    try:
        report = build_report(
            record,
            items,
            meta=meta,
            gate=_gate_for(items, record.config.judge_models, record.mock),
            baseline=baseline,
            rules=StatRules(
                margin=margin, min_tasks=min_tasks, n_boot=resamples, seed=record.config.seed
            ),
            spend_total_usd=spend,
            retried_after_halt=store.retried_after_halt(run_id),
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    run_dir = store.run_dir(run_id)
    if format == "html":
        target = output or run_dir / "report.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_html(report, run_dir), encoding="utf-8")
        console.print(f"Wrote {target} ({target.stat().st_size // 1024} KB, self-contained)")
        return
    text = render_markdown(report) if format == "md" else report.model_dump_json(indent=2)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        console.print(f"Wrote {output}")
    else:
        typer.echo(text)


@bench_app.command("compare")
def compare_cmd(
    before: Annotated[str, typer.Argument(help="The earlier run")],
    after: Annotated[str, typer.Argument(help="The later run (after a tuning change)")],
    format: _Format = "md",
    margin: _Margin = DEFAULT_MARGIN,
    min_tasks: _MinTasks = DEFAULT_MIN_TASKS,
    resamples: _Resamples = DEFAULT_BOOTSTRAP,
) -> None:
    """Compare two runs of the same arms: is the later one cheaper, faster, not worse, better?"""
    if format not in ("md", "json"):
        msg = f"--format must be md or json, not '{format}'"
        raise ConfigError(msg)
    store = BenchStore(bench_results_dir())
    first, second = _run_inputs(store, before), _run_inputs(store, after)
    result = compare_runs(
        first,
        second,
        rules=StatRules(
            margin=margin, min_tasks=min_tasks, n_boot=resamples, seed=first[0].config.seed
        ),
    )
    typer.echo(render_comparison(result) if format == "md" else result.model_dump_json(indent=2))


@bench_app.command("recommend")
def recommend_cmd(
    run_id: Annotated[str, typer.Argument(help="A dev ablation run")],
    among: Annotated[
        str | None,
        typer.Option(help="Candidate arms, comma-separated (default: every arm but solo ones)"),
    ] = None,
    margin: _Margin = DEFAULT_MARGIN,
    name: Annotated[str, typer.Option(help="Name for the suite-file arm line")] = "fusion-best",
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON")] = False,
) -> None:
    """Which arm to make the default, from a dev run: the cheapest per solved task of the
    cost-quality frontier within the margin of the best. Prints the arm line for the suite."""
    store = BenchStore(bench_results_dir())
    _, items, meta = _run_inputs(store, run_id)
    arms_meta = meta.arms if meta else []
    solo = {a.name for a in arms_meta if a.kind == "solo"}
    wanted = [a.strip() for a in among.split(",")] if among else None
    stats = study_stats(items, rules=StatRules(n_boot=DEFAULT_BOOTSTRAP))
    pool = wanted or [a.arm for a in stats.arms if a.arm not in solo]
    advice = recommend(stats.arms, among=pool, margin=margin, arm_meta=arms_meta)
    if as_json:
        typer.echo(advice.model_dump_json(indent=2))
        return
    table = Table(title="Candidates")
    for column in ("Arm", "Quality (95% CI)", "$/task", "$/solved", "Frontier", "Within margin"):
        table.add_column(column)
    for c in advice.candidates:
        low, high = c.quality_low, c.quality_high
        shown = f"{c.quality:.3f}" + (
            f" [{low:.3f}, {high:.3f}]" if low is not None and high is not None else ""
        )
        table.add_row(
            c.arm,
            shown,
            f"{c.cost_per_task:.4f}",
            "-" if c.cost_per_solved is None else f"{c.cost_per_solved:.4f}",
            "yes" if c.on_frontier else "no",
            "yes" if c.within_margin else "no",
        )
    console.print(table)
    console.print(advice.reason, markup=False)
    line = advice.arm_line(name)
    if line:
        console.print("For the headline suite (replace its fusion-best line):", markup=False)
        typer.echo(line)


@bench_app.command("results")
def results_cmd(
    run_id: Annotated[str, typer.Argument(help="The headline run")],
    also: Annotated[
        list[str] | None,
        typer.Option("--also", help="Supporting runs (ablation, latency); repeat the option"),
    ] = None,
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Where to write the document")
    ] = Path("docs/BENCHMARK_RESULTS.md"),
    html: Annotated[
        Path | None,
        typer.Option(
            "--html", help="Also write the HTML report here and link it from the document"
        ),
    ] = None,
    svg: Annotated[
        Path | None,
        typer.Option("--svg", help="Also write the cost-quality chart as a stand-alone SVG"),
    ] = None,
    baseline: Annotated[str | None, typer.Option(help="Baseline arm of the headline")] = None,
    margin: _Margin = DEFAULT_MARGIN,
    min_tasks: _MinTasks = DEFAULT_MIN_TASKS,
    resamples: _Resamples = DEFAULT_BOOTSTRAP,
) -> None:
    """Write docs/BENCHMARK_RESULTS.md from runs: verdicts verbatim, spend, limitations."""
    store = BenchStore(bench_results_dir())

    def make(rid: str, base: str | None) -> tuple[Report, Path]:
        record, items, meta = _run_inputs(store, rid)
        rules_here = StatRules(
            margin=margin, min_tasks=min_tasks, n_boot=resamples, seed=record.config.seed
        )
        try:
            built = build_report(
                record,
                items,
                meta=meta,
                gate=_gate_for(items, record.config.judge_models, record.mock),
                baseline=base,
                rules=rules_here,
                retried_after_halt=store.retried_after_halt(rid),
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        return built, store.run_dir(rid)

    headline, run_dir = make(run_id, baseline)
    supporting = [make(r, None)[0] for r in also or []]
    link: str | None = None
    if html is not None:
        html.parent.mkdir(parents=True, exist_ok=True)
        html.write_text(render_html(headline, run_dir), encoding="utf-8")
        link = os.path.relpath(html, output.parent)
        console.print(f"Wrote {html}")
    if svg is not None:
        svg.parent.mkdir(parents=True, exist_ok=True)
        svg.write_text(cost_quality_svg(headline), encoding="utf-8")
        console.print(f"Wrote {svg}")
    ledger = None if headline.run.mock else SpendLedger(store.root / SPEND_FILE).entries()
    commands = [
        f"uv run fusion bench report {run_id} --format html",
        f"uv run fusion bench results {run_id}"
        + "".join(f" --also {r}" for r in also or [])
        + (f" --html {html}" if html else "")
        + (f" --svg {svg}" if svg else ""),
    ]
    text = render_results(headline, supporting, ledger=ledger, html_link=link, commands=commands)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    console.print(f"Wrote {output}")


@bench_app.command("calibrate-judge")
def calibrate_judge_cmd(
    dataset: _Dataset = "",
    judge_models: Annotated[
        str | None, typer.Option(help="Catalog aliases of the judges (--mock picks three)")
    ] = None,
    cases: Annotated[
        Path | None,
        typer.Option(
            help="JSONL of {task_id, good, flawed}; default: seeded from each task's truth"
        ),
    ] = None,
    artifacts: Annotated[
        bool,
        typer.Option(
            "--artifacts",
            help="Calibrate the agentic judge on frontend and performance outputs "
            "(solutions against deliberately broken pages and known-slow code)",
        ),
    ] = False,
    cases_per_set: Annotated[
        int | None,
        typer.Option(help="With --artifacts: cases per set (visual, perf); default 6"),
    ] = None,
    accuracy_floor: Annotated[
        float | None,
        typer.Option(
            "--accuracy-floor",
            help="With --artifacts: accuracy a judge needs for a headline verdict "
            "(default FUSION_JUDGE_ACCURACY_FLOOR, else 0.8)",
        ),
    ] = None,
    max_usd: _MaxUsd = None,
    mock: _Mock = False,
    seed: _Seed = None,
    concurrency: _Concurrency = None,
    limit: _Limit = None,
    split: _Split = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON")] = False,
) -> None:
    """Measure how well each judge picks the better of two answers whose order is known."""
    if not dataset:
        err.print("Name a dataset with --dataset.")
        raise typer.Exit(2)
    base_seed = seed or 0
    probe = BenchConfig(
        dataset=Path(dataset), arms=parse_arms("solo-cheap"), max_usd=max_usd or 1.0, mock=mock
    )
    tasks = load_dataset(dataset, split or "dev")
    if not artifacts:
        tasks = select_tasks(tasks, limit, base_seed)
    try:
        env = build_env(probe.model_copy(update={"dataset": resolve_dataset(dataset)}), tasks)
    except RuntimeError as exc:  # no provider is configured
        raise ConfigError(str(exc)) from exc
    judges = [j.strip() for j in (judge_models or "").split(",") if j.strip()]
    if not judges and mock:
        judges = default_mock_judges(env)
    if not judges:
        err.print("Name the judges with --judge-models (catalog aliases, comma-separated).")
        raise typer.Exit(2)

    async def study() -> CalibrationReport:
        try:
            if artifacts:
                return await run_artifact_calibration(
                    env,
                    tasks,
                    artifact_cases(tasks, per_set=cases_per_set or 6, seed=base_seed),
                    judges,
                    max_usd=max_usd,
                    seed=base_seed,
                    mock=mock,
                    floor=accuracy_floor,
                )
            pairs = cases_from_file(cases) if cases else build_cases(tasks)
            return await run_calibration(
                env,
                tasks,
                pairs,
                judges,
                max_usd=max_usd,
                seed=base_seed,
                concurrency=concurrency or 8,
                mock=mock,
            )
        finally:
            await close_providers(env.providers)

    try:
        report = run_virtual(study()) if mock else asyncio.run(study())
    except (ScoringError, SpendCapError) as exc:
        err.print(str(exc))
        raise typer.Exit(2) from exc
    path = save_report(report, env.store.root)
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
        return
    _print_calibration(report, path)


def _print_calibration(report: CalibrationReport, path: Path) -> None:
    table = Table(title=f"Judge calibration {report.id} ({report.cases} cases)")
    sets = sorted(report.by_set)
    columns = ["judge", "accuracy", "ties", "order flips", "kappa", "failed calls"]
    for column in [*columns, *(f"{s} acc." for s in sets)]:
        table.add_column(column, justify="left" if column == "judge" else "right")
    for j in report.judges:
        table.add_row(
            j.judge,
            f"{j.accuracy:.0%}",
            f"{j.tie_rate:.0%}",
            f"{j.inconsistent_rate:.0%}",
            f"{j.kappa:.2f}",
            str(j.failed_calls),
            *(f"{report.by_set[s].get(j.judge, 0.0):.0%}" for s in sets),
        )
    console.print(table)
    if report.agreement is not None:
        console.print(f"Judges agree on {report.agreement:.0%} of cases.")
        for pair, kappa in report.pair_kappa.items():
            console.print(f"  kappa {pair}: {kappa:.2f}")
    if report.kind == "artifact":
        below = [j.judge for j in report.judges if not j.meets(report.floor)]
        if below:
            console.print(
                f"[bold red]Below the {report.floor:.0%} accuracy floor:[/bold red] "
                f"{', '.join(below)}. Studies that use them get no headline verdict."
            )
        else:
            console.print(f"Every judge meets the {report.floor:.0%} accuracy floor.")
    console.print(
        f"Cost ${report.cost_usd:.4f}"
        + (" (simulated judges: not a measurement)" if report.mock else "")
        + f"; saved to {path}"
    )


# -------------------------------------------------------------------------------- datasets

dataset_app = typer.Typer(
    help="Validate, summarise and build benchmark datasets", no_args_is_help=True
)
bench_app.add_typer(dataset_app, name="dataset")
_DatasetPath = Annotated[str, typer.Argument(help="Dataset file, directory or name")]


@dataset_app.command("validate")
def dataset_validate_cmd(
    path: _DatasetPath,
    release: Annotated[
        bool,
        typer.Option("--release", help="Also require the published dataset's size and coverage"),
    ] = False,
    quiet: Annotated[bool, typer.Option("--quiet", help="Hide warnings")] = False,
) -> None:
    """Check a dataset: schema, ids, line numbers, secrets, splits, and that truth is scorable."""
    report = validate_dataset(path, Rules(release=release))
    for issue in report.issues:
        if issue.level == "error" or not quiet:
            (err if issue.level == "error" else console).print(f"{issue}", markup=False)
    n_err, n_warn = len(report.errors), len(report.warnings)
    if report.ok:
        console.print(
            f"[green]OK[/green]: {report.stats.tasks} tasks"
            + (f", {n_warn} warnings" if n_warn else "")
            + (" (release rules)" if release else "")
        )
        return
    err.print(f"{n_err} errors, {n_warn} warnings in {path}")
    raise typer.Exit(1)


@dataset_app.command("stats")
def dataset_stats_cmd(path: _DatasetPath) -> None:
    """Counts per category, difficulty, split and language, and the size of the tasks."""
    stats = validate_dataset(path, Rules(run_code=False)).stats  # counting needs no test runs
    table = Table(title=f"{path}: {stats.tasks} tasks")
    for column in (
        "category",
        "tasks",
        "easy",
        "medium",
        "hard",
        "dev",
        "test",
        "lines min/mean/max",
    ):
        table.add_column(column, justify="left" if column == "category" else "right")
    for category, n in stats.by_category.items():
        diff, split = stats.by_difficulty[category], stats.by_split[category]
        lines = stats.lines.get(category)
        table.add_row(
            category,
            str(n),
            *(str(diff.get(d, 0)) for d in ("easy", "medium", "hard")),
            *(str(split.get(x, 0)) for x in ("dev", "test")),
            "/".join(map(str, lines)) if lines else "-",
        )
    console.print(table)
    if stats.languages:
        console.print("Languages: " + ", ".join(f"{k} {v}" for k, v in stats.languages.items()))
    if stats.reviews:
        console.print(
            f"Clean code-review tasks: {stats.clean_reviews} of {stats.reviews} "
            f"({stats.clean_reviews / stats.reviews:.0%})"
        )


@dataset_app.command("build")
def dataset_build_cmd(
    authoring: Annotated[
        Path, typer.Option(help="Directory of authoring YAML files")
    ] = AUTHORING_DIR,
    out: Annotated[Path, typer.Option(help="Where the JSONL goes")] = Path("evals/datasets/v1"),
    check: Annotated[
        bool, typer.Option("--check", help="Write nothing; fail if the JSONL is out of date")
    ] = False,
    generate: Annotated[
        int | None,
        typer.Option(help="Instead, have a model draft this many candidate tasks (costs money)"),
    ] = None,
    category: Annotated[str, typer.Option(help="Category to draft (with --generate)")] = "",
    model: Annotated[str, typer.Option(help="Catalog alias that drafts (with --generate)")] = "",
    max_usd: _MaxUsd = None,
    candidates: Annotated[
        Path | None, typer.Option(help="Where drafts are written (with --generate)")
    ] = None,
) -> None:
    """Compile authoring files into the dataset JSONL, or draft candidates for review."""
    if generate is not None:
        _generate(generate, category, model, max_usd, candidates, authoring, out)
        return
    try:
        compiled = compile_dir(authoring)
    except AuthoringError as exc:
        err.print(str(exc), markup=False)
        raise typer.Exit(1) from exc
    if check:
        stale = [
            str(out / CATEGORY_FILES[c])
            for c, tasks in compiled.tasks.items()
            if not (out / CATEGORY_FILES[c]).is_file()
            or (out / CATEGORY_FILES[c]).read_text(encoding="utf-8") != render_jsonl(tasks)
        ]
        if stale:
            err.print("Out of date (run `fusion bench dataset build`): " + ", ".join(stale))
            raise typer.Exit(1)
        console.print(f"Up to date: {len(compiled.all())} tasks in {out}")
        return
    paths = write_compiled(compiled, out)
    console.print(f"Wrote {len(compiled.all())} tasks to " + ", ".join(p.name for p in paths))


def _generate(
    count: int,
    category: str,
    model: str,
    max_usd: float | None,
    candidates: Path | None,
    authoring: Path,
    out: Path,
) -> None:
    if not category or not model or max_usd is None:
        err.print("--generate needs --category, --model and --max-usd.")
        raise typer.Exit(2)
    probe = BenchConfig(dataset=Path("."), arms=parse_arms("solo-cheap"), max_usd=max_usd)
    try:
        env = build_env(probe, [])
        existing = load_dataset(out) if out.exists() else []
    except RuntimeError as exc:  # no provider is configured
        raise ConfigError(str(exc)) from exc

    async def drafting() -> Generated:
        try:
            return await run_generation(
                env, category, count, alias=model, max_usd=max_usd, existing=existing
            )
        finally:
            await close_providers(env.providers)

    try:
        result = asyncio.run(drafting())
    except (AuthoringError, SpendCapError) as exc:
        err.print(str(exc), markup=False)
        raise typer.Exit(2) from exc
    target = candidates or authoring.parent / "candidates" / f"{category}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    header = "# UNREVIEWED model drafts. Check every seeded defect and rubric point by hand.\n"
    target.write_text(header + yaml.safe_dump(result.items, sort_keys=False, allow_unicode=True))
    console.print(
        f"{len(result.items)} drafts written to {target} (${result.cost_usd:.4f}); "
        f"{len(result.rejected)} rejected"
    )
    for reason in result.rejected:
        err.print(reason, markup=False)


suite_app = typer.Typer(help="The packaged studies (ablations, the headline run, latency)")
bench_app.add_typer(suite_app, name="suite")


@suite_app.command("list")
def suite_list_cmd() -> None:
    """List the packaged suites."""
    table = Table(title="Suites")
    for column in ("Suite", "Stage", "Split", "Arms", "Repeats", "Budget", "What it answers"):
        table.add_column(column)
    for suite in list_suites():
        cfg = suite.config
        table.add_row(
            suite.name,
            suite.stage,
            str(cfg.get("split", "dev")),
            str(len(suite.arms)),
            str(cfg.get("repeats", 3)),
            f"${float(cfg.get('max_usd', 0)):g}",
            suite.description,
        )
    console.print(table)
    console.print("Price one with: fusion bench plan --suite NAME   (run it with --suite NAME)")


@suite_app.command("show")
def suite_show_cmd(name: Annotated[str, typer.Argument(help="Suite name or YAML file")]) -> None:
    """Show a suite: what it runs, on which tasks, and each arm's strategy and overrides."""
    suite = load_suite(name)
    cfg = suite.config
    console.print(f"[bold]{suite.name}[/bold] ({suite.stage}): {suite.description}", markup=True)
    console.print(f"File: {suite.path}")
    quota = cfg.get("quota") or {}
    tasks = f"{sum(quota.values())} tasks ({', '.join(f'{c} {n}' for c, n in quota.items())})"
    judges = ", ".join(cfg.get("judge_models") or []) or "none"
    console.print(
        f"Dataset {cfg.get('dataset')}, split {cfg.get('split', 'dev')}: "
        f"{tasks if quota else 'all tasks'}",
        markup=False,
    )
    console.print(
        f"{cfg.get('repeats', 3)} repeat(s), seed {cfg.get('seed', 0)}; budget "
        f"${cfg.get('max_usd')}, stop at ${cfg.get('spend_stop_usd', 20)} cumulative; "
        f"judges {judges}; response cache {'on' if cfg.get('cache', True) else 'off'}",
        markup=False,
    )
    table = Table(title="Arms")
    for column in ("Arm", "Strategy", "Overrides"):
        table.add_column(column)
    for arm in cfg.get("arms", []):
        overrides = arm.get("overrides") or {}
        table.add_row(arm["name"], arm["strategy"], json.dumps(overrides) if overrides else "")
    console.print(table)


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
