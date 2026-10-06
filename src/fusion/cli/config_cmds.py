"""``fusion config ...`` and ``fusion strategies ...``: look at and check the configuration."""

from __future__ import annotations

import json
import os
from typing import Annotated

import typer
from rich.table import Table

from fusion.cli.common import PROVIDER_KEYS, JsonOption, console, echo_json
from fusion.config import paths as fusion_paths
from fusion.config.catalog import catalog_warnings, load_catalog
from fusion.config.layers import flatten, resolve_config
from fusion.config.loader import load_baseline, load_routing_policies
from fusion.orchestration.strategy import load_strategy_book

config_app = typer.Typer(help="Validate Fusion configuration")
strategies_app = typer.Typer(help="Inspect strategies (the panels Fusion can run)")


@config_app.command("paths")
def config_paths(as_json: JsonOption = False) -> None:
    """Show where Fusion reads configuration and keeps its database."""
    rows = [
        ("user config", fusion_paths.user_config_file()),
        ("project config", fusion_paths.project_config_file()),
        ("database", fusion_paths.resolve_db_path()),
        ("legacy database", fusion_paths.legacy_db_path()),
    ]
    if as_json:
        echo_json(
            [{"name": label, "path": str(path), "exists": path.exists()} for label, path in rows]
        )
        return
    for label, path in rows:
        state = "exists" if path.exists() else "missing"
        typer.echo(f"{label:<16} {path}  ({state})")


@config_app.command("show")
def config_show(
    resolved: Annotated[
        bool, typer.Option("--resolved", help="Print every merged value with its origin")
    ] = False,
    as_json: JsonOption = False,
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
def strategies_list(as_json: JsonOption = False) -> None:
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
    as_json: JsonOption = False,
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

    for provider, env_name in PROVIDER_KEYS.items():
        enabled = any(m.enabled and m.provider == provider for m in models.values())
        if enabled and not os.environ.get(env_name):
            message = f"{env_name} missing for enabled {provider} models"
            (issues if strict else warnings).append(message)

    if as_json:
        echo_json({"valid": not issues, "errors": issues, "warnings": warnings})
        if issues:
            raise typer.Exit(1)
        return
    if issues:
        for issue in issues:
            console.print(f"[red]ERROR[/red] {issue}")
        raise typer.Exit(1)
    for warning in warnings:
        console.print(f"[yellow]WARN[/yellow] {warning}")
    console.print("[green]Configuration valid[/green]")
