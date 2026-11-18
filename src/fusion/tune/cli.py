"""``fusion tune``: tune the prompt pack on the dev split (Propose–Probe–Compose)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from fusion.bench.arms import parse_arms
from fusion.bench.plan import make_plan
from fusion.bench.runner import BenchEnv, build_env
from fusion.bench.spec import BenchConfig, BenchTask, load_dataset, select_tasks
from fusion.bench.virtual import run_virtual
from fusion.config.layers import ConfigError
from fusion.config.paths import bench_results_dir
from fusion.library.distil import pick_distil_model
from fusion.providers.base import close_providers
from fusion.providers.simulated_tune import sim_prompt_effects
from fusion.routing.policy import RoutingPolicy
from fusion.tune.probe import split_holdout
from fusion.tune.propose import ModelTuner
from fusion.tune.run import TuneConfig, TuneResult, run_tune

__all__ = ["tune_cmd"]

console = Console()
err = Console(stderr=True)


def _probe_forecast(cfg: TuneConfig, env: BenchEnv, tasks: list[BenchTask]) -> float:
    """What one probe of the tuning slice is expected to cost (the bench planner's estimate)."""
    bench = BenchConfig(
        dataset=cfg.dataset,
        arms=parse_arms(cfg.arm),
        repeats=cfg.repeats,
        max_usd=cfg.max_usd or 1.0,
        mock=cfg.mock,
    )
    routing = RoutingPolicy(env.routing_config, registry=env.registry, strategies=env.book)
    plan = make_plan(
        bench,
        tasks,
        {cfg.arm: env.book.get(cfg.arm)},
        routing=routing,
        registry=env.registry,
        pricing=env.pricing,
    )
    return plan.expected_usd


def _print(result: TuneResult) -> None:
    table = Table(title="Evidence")
    for column in ("id", "stage", "prompt(s)", "edit", "Δ quality", "band", "verdict"):
        table.add_column(column, overflow="fold")
    for row in result.evidence:
        eff = row.effect
        table.add_row(
            row.id,
            row.stage,
            ", ".join(row.targets),
            row.edit,
            eff.text() if eff else "",
            f"{eff.band:.3f}" if eff else "",
            row.verdict + (f": {'; '.join(row.errors)}" if row.errors else ""),
        )
    console.print(table)
    base = f"{result.base_score:.3f}" if result.base_score is not None else "n/a"
    console.print(
        f"Tuning {result.tune_id}: {result.status}, {result.probes} probe(s), "
        f"${result.spent_usd:.4f} spent; base pack scored {base}."
    )
    if result.stop_reason:
        console.print(f"Stopped early: {result.stop_reason}")
    out = Path(result.out_dir)
    if result.adopted_digest:
        console.print(f"Adopted pack {result.adopted_digest}: diff in {out / 'pack.diff'}.")
        console.print(f"To use it: FUSION_PROMPT_PACK={out / 'pack'}")
    else:
        console.print("Nothing adopted: the base pack is kept.")
    console.print(f"Evidence table: {out / 'evidence.md'}")


def tune_cmd(
    dataset: Annotated[
        str, typer.Option("--dataset", "-d", help="Dataset with a dev split (default v1)")
    ] = "v1",
    arm: Annotated[str, typer.Option(help="The strategy whose answers are scored")] = "auto",
    mode: Annotated[
        str, typer.Option(help="wide: K proposers per axis, then K composers; deep: K rounds")
    ] = "wide",
    k: Annotated[int, typer.Option("--k", help="Proposers per axis and composers, or rounds")] = 3,
    axes: Annotated[str, typer.Option(help="inv (procedure), dom (domain), or both")] = "inv,dom",
    repeats: Annotated[int, typer.Option(help="Runs of each task per probe")] = 1,
    limit: Annotated[
        int, typer.Option(help="Dev tasks used (tuning + held out), spread over categories")
    ] = 24,
    holdout: Annotated[
        float, typer.Option(help="Share of those tasks held out for the one confirmation")
    ] = 0.25,
    min_gain: Annotated[float, typer.Option(help="Smallest quality gain that counts")] = 0.02,
    min_category_tasks: Annotated[
        int, typer.Option(help="Tasks a task type needs before its effect can confound an edit")
    ] = 2,
    seed: Annotated[int, typer.Option(help="Seed for sampling, holdout and bootstrap")] = 0,
    max_usd: Annotated[
        float | None,
        typer.Option("--max-usd", help="Spending limit for the whole run (required unless --mock)"),
    ] = None,
    mock: Annotated[
        bool, typer.Option("--mock", help="Simulated models with planted prompt effects; free")
    ] = False,
    tuner_model: Annotated[
        str | None, typer.Option(help="Catalog alias that proposes and composes (default: a judge)")
    ] = None,
    concurrency: Annotated[int, typer.Option(help="Jobs at once within a probe")] = 8,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Do not reuse or store responses")
    ] = False,
) -> None:
    """Tune the prompt pack on the dev split: propose edits, probe them, compose a final.

    Writes a diff of the pack and an evidence table under bench-results/tune/. The test split is
    never used. Nothing changes in use until you point FUSION_PROMPT_PACK at the adopted pack.
    """
    try:
        cfg = TuneConfig(
            dataset=Path(dataset),
            arm=arm,
            mode=mode,  # type: ignore[arg-type]
            k=k,
            axes=[a.strip() for a in axes.split(",") if a.strip()],  # type: ignore[misc]
            repeats=repeats,
            limit=limit,
            holdout_share=holdout,
            min_gain=min_gain,
            min_category_tasks=min_category_tasks,
            seed=seed,
            max_usd=max_usd,
            mock=mock,
            tuner_model=tuner_model,
            concurrency=concurrency,
            cache=not no_cache,
        )
    except ValueError as exc:
        raise ConfigError(str(exc).splitlines()[-1].strip()) from exc
    dev = select_tasks(load_dataset(cfg.dataset, "dev"), cfg.limit, cfg.seed)
    bench = BenchConfig(
        dataset=cfg.dataset,
        arms=parse_arms(cfg.arm),
        max_usd=cfg.max_usd or 1.0,
        mock=cfg.mock,
        cache=cfg.cache,
        seed=cfg.seed,
    )
    try:
        env = build_env(bench, dev, prompt_effects=sim_prompt_effects() if cfg.mock else None)
    except RuntimeError as exc:  # no provider is configured
        raise ConfigError(str(exc)) from exc
    env.book.get(cfg.arm)  # an unknown arm fails here, before anything runs
    model = pick_distil_model(
        configured=cfg.tuner_model,
        judge=None,
        judges=env.registry.by_role("judge"),
        models=env.registry.models,
        providers=env.providers,
    )
    if model is None:
        raise ConfigError("No catalog model with a configured provider can propose edits")
    tuner = ModelTuner(
        model,
        models=env.registry.models,
        providers=env.providers,
        pricing=env.pricing,
        seed=cfg.seed,
    )
    tuning, _ = split_holdout(dev, cfg.holdout_share, cfg.seed)
    per_probe = _probe_forecast(cfg, env, tuning)
    most = 1 + len(cfg.axes) * cfg.k + cfg.k + 2 if cfg.mode == "wide" else 1 + cfg.k + 2
    err.print(
        f"Tuning `{cfg.arm}` on {len(dev)} dev tasks of {cfg.dataset} ({cfg.mode}, k={cfg.k}), "
        f"tuner {model}: at most {most} probes at about ${per_probe:.4f} each"
        + (" (simulated, spends nothing)." if cfg.mock else f", capped at ${cfg.max_usd:.2f}.")
    )
    tune_id = f"tune-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    out_dir = bench_results_dir() / "tune" / tune_id

    async def go() -> TuneResult:
        try:
            return await run_tune(cfg, env=env, tuner=tuner, out_dir=out_dir, tune_id=tune_id)
        finally:
            await close_providers(env.providers)

    with err.status("tuning"):
        result = run_virtual(go()) if cfg.mock else asyncio.run(go())
    _print(result)
