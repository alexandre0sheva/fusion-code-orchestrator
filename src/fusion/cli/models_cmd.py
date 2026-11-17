"""`fusion models`: inspect and verify the model catalog."""

from __future__ import annotations

import asyncio
import os
from datetime import date, datetime
from typing import Annotated, Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from fusion.cli.common import JsonOption, echo_json
from fusion.config.catalog import (
    FREE_PROVIDERS,
    Catalog,
    ModelEntry,
    catalog_warnings,
    load_catalog,
)
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


def tier_changes(entry: ModelEntry, on: date) -> list[dict[str, str]]:
    """Dates after ``on`` where a scheduled price change moves the model to another tier."""
    changes: list[dict[str, str]] = []
    tier = entry.price_tier(on)
    starts = sorted(
        {p.effective_from for p in entry.prices if p.effective_from and p.effective_from > on}
    )
    for start in starts:
        new_tier = entry.price_tier(start)
        if new_tier != tier:
            changes.append({"on": start.isoformat(), "tier": new_tier})
            tier = new_tier
    return changes


def _row(catalog: Catalog, alias: str, on: date) -> dict[str, Any]:
    entry = catalog.models[alias]
    price, verified = _price_text(catalog, alias, on)
    blended = entry.blended_price(on)
    return {
        "alias": alias,
        "provider": entry.provider,
        "model_id": entry.model_id,
        "enabled": entry.enabled,
        "roles": list(entry.roles),
        "price": price,
        "blended": round(blended, 4) if blended is not None else None,
        "tier": entry.price_tier(on),
        "tier_changes": tier_changes(entry, on),
        "verified": verified or None,
    }


def _parse_day(value: str | None) -> date:
    if value is None:
        return date.today()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise typer.BadParameter("use YYYY-MM-DD", param_hint="--on") from exc


@models_app.command("list")
def models_list(
    all_models: Annotated[
        bool, typer.Option("--all", help="Include disabled and mock models")
    ] = False,
    on: Annotated[
        str | None, typer.Option("--on", help="Price the catalog on this date (YYYY-MM-DD)")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """List catalog models with IDs, prices, blended price, price tier and verification dates.

    The blended price is (3 x input + output) / 4 per 1M tokens (weights from the catalog's
    `tiering:` block); the tier follows from it. A scheduled price change that moves a model to
    another tier is shown next to its tier.
    """
    catalog = load_catalog()
    day = _parse_day(on)
    aliases = [
        alias
        for alias, entry in catalog.models.items()
        if all_models or (entry.enabled and entry.provider != "mock")
    ]
    rows = [_row(catalog, alias, day) for alias in aliases]
    if as_json:
        echo_json(rows)
        return
    table = Table(title=f"Fusion model catalog on {day} (USD per 1M tokens)")
    columns = ("Alias", "Provider", "Model ID", "In / out", "Blended", "Tier", "Verified", "Roles")
    for column in columns:
        table.add_column(column)
    for row in rows:
        free = row["provider"] in FREE_PROVIDERS
        tier = row["tier"] + "".join(
            f" ({change['tier']} from {change['on']})" for change in row["tier_changes"]
        )
        table.add_row(
            row["alias"],
            row["provider"],
            row["model_id"],
            "free" if free else row["price"],
            "" if free or row["blended"] is None else f"${row['blended']:.2f}",
            tier,
            row["verified"] or "",
            ", ".join(row["roles"]),
        )
    Console(width=max(console.width, 150)).print(table)


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
            "newer": "[yellow]NEWER[/yellow]",
            "redirect": "[yellow]MOVED[/yellow]",
        }[item.status]
        detail = f" ({item.detail})" if item.detail else ""
        console.print(f"{label} {item.alias}: {item.provider}/{item.model_id}{detail}")
        if item.status not in {"ok", "skipped"}:
            problems += 1
    return problems


@models_app.command("check")
def models_check(
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help=(
                "Also ask each provider's free list-models endpoint (needs API keys) for unknown "
                "IDs and newer models, and check that price pages have not moved"
            ),
        ),
    ] = False,
    strict: Annotated[bool, typer.Option(help="Exit with an error if anything is flagged")] = False,
    as_json: JsonOption = False,
) -> None:
    """Check the catalog: stale prices, price changes within 30 days, retiring models, and
    default models that are not the newest of their family."""
    catalog = load_catalog()
    warnings = catalog_warnings(catalog)
    if not as_json:
        for warning in warnings:
            console.print(f"[yellow]WARN[/yellow] {warning}")
    problems = len(warnings)

    if live:
        keys = _api_keys()

        async def _run() -> list[ModelCheck]:
            async with httpx.AsyncClient() as client:
                return await check_catalog_live(catalog, keys, client)

        checked = asyncio.run(_run())
        if as_json:
            problems += sum(item.status not in {"ok", "skipped"} for item in checked)
            echo_json(
                {
                    "warnings": warnings,
                    "live": [item.model_dump() for item in checked],
                    "problems": problems,
                }
            )
        else:
            problems += _print_live(checked)
    elif as_json:
        echo_json({"warnings": warnings, "live": None, "problems": problems})
    elif not warnings:
        console.print(
            "[green]Catalog looks current.[/green] Use --live to verify IDs with providers."
        )

    if strict and problems:
        raise typer.Exit(1)
