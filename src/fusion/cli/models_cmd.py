"""`fusion models`: inspect and verify the model catalog."""

from __future__ import annotations

import asyncio
import os
from datetime import date
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.table import Table

from fusion.config.catalog import FREE_PROVIDERS, Catalog, catalog_warnings, load_catalog
from fusion.config.catalog_check import ModelCheck, check_catalog_live

models_app = typer.Typer(help="Inspect and verify the model catalog (IDs and prices)")
console = Console()

_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def _price_text(catalog: Catalog, alias: str, today: date) -> tuple[str, str]:
    schedule = catalog.models[alias].price_at(today)
    if schedule is None:
        return "no price in effect", ""
    verified = schedule.verified_on.isoformat() if schedule.verified_on else "n/a"
    return f"${schedule.input_per_1m:g} / ${schedule.output_per_1m:g}", verified


@models_app.command("list")
def models_list(
    all_models: Annotated[
        bool, typer.Option("--all", help="Include disabled and mock models")
    ] = False,
) -> None:
    """List catalog models with provider IDs, prices (per 1M tokens) and verification dates."""
    catalog = load_catalog()
    today = date.today()
    table = Table(title="Fusion model catalog (USD per 1M tokens: input / output)")
    for column in ("Alias", "Provider", "Model ID", "Price in/out", "Verified", "Roles"):
        table.add_column(column)
    for alias, entry in catalog.models.items():
        if not all_models and (not entry.enabled or entry.provider == "mock"):
            continue
        price, verified = _price_text(catalog, alias, today)
        table.add_row(
            alias,
            entry.provider,
            entry.model_id,
            price if entry.provider not in FREE_PROVIDERS else "free",
            verified,
            ", ".join(entry.roles),
        )
    Console(width=max(console.width, 118)).print(table)


def _api_keys() -> dict[str, str]:
    return {
        provider: os.environ[env]
        for provider, env in _KEY_ENV.items()
        if os.environ.get(env, "").strip()
    }


def _print_live(results: list[ModelCheck]) -> int:
    problems = 0
    for item in results:
        label = {
            "ok": "[green]OK[/green]",
            "unknown_id": "[red]UNKNOWN[/red]",
            "skipped": "[dim]SKIP[/dim]",
            "error": "[yellow]ERROR[/yellow]",
        }[item.status]
        detail = f" ({item.detail})" if item.detail else ""
        console.print(f"{label} {item.alias}: {item.provider}/{item.model_id}{detail}")
        if item.status in {"unknown_id", "error"}:
            problems += 1
    return problems


@models_app.command("check")
def models_check(
    live: Annotated[
        bool,
        typer.Option(
            "--live", help="Also ask each provider's free list-models endpoint (needs API keys)"
        ),
    ] = False,
    strict: Annotated[bool, typer.Option(help="Exit with an error if anything is flagged")] = False,
) -> None:
    """Check the catalog for stale prices, upcoming price changes and retiring models."""
    catalog = load_catalog()
    warnings = catalog_warnings(catalog)
    for warning in warnings:
        console.print(f"[yellow]WARN[/yellow] {warning}")
    problems = len(warnings)

    if live:
        keys = _api_keys()

        async def _run() -> list[ModelCheck]:
            async with httpx.AsyncClient() as client:
                return await check_catalog_live(catalog, keys, client)

        problems += _print_live(asyncio.run(_run()))
    elif not warnings:
        console.print(
            "[green]Catalog looks current.[/green] Use --live to verify IDs with providers."
        )

    if strict and problems:
        raise typer.Exit(1)
