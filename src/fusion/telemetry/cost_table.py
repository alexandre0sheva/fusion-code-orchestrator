"""The strategy cost table in docs/COSTS.md, computed from the catalog instead of typed by hand.

Each row prices one strategy for a typical task with the same arithmetic the pre-flight budget
check uses (``fusion.routing.budget``): every call reads 5,000 tokens and every answer is 1,500
tokens, at the catalog prices in effect on the table's date. It is an illustration of how
strategies compare, not a measurement; a real run's cost comes from its ledger.

Regenerate with ``uv run python evals/runners/cost_table.py --write``. ``tests/test_cost_table.py``
fails when the table in the doc no longer matches the catalog and the packaged strategies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from fusion.config.catalog import FREE_PROVIDERS, Catalog
from fusion.orchestration.budget_guard import plan_calls
from fusion.orchestration.strategy import Strategy, StrategyBook
from fusion.routing.budget import PROMPT_OVERHEAD_TOKENS, forecast_calls
from fusion.telemetry.cost import PricingRegistry

__all__ = [
    "INPUT_TOKENS_PER_CALL",
    "CostRow",
    "cost_rows",
    "render_cost_table",
    "table_date",
    "update_document",
]

INPUT_TOKENS_PER_CALL = 5_000
REFERENCE = "solo-frontier"
_JUDGE_MODEL = "gemini-flash"
_START = re.compile(r"<!-- cost-table:start as-of=(\d{4}-\d{2}-\d{2}) -->")
_BLOCK = re.compile(r"(<!-- cost-table:start as-of=\S+ -->\n).*?(<!-- cost-table:end -->)", re.S)


@dataclass(frozen=True)
class CostRow:
    label: str
    calls: int
    usd: float


def _cost(
    strategy: Strategy, catalog: Catalog, pricing: PricingRegistry, *, escalate: bool
) -> CostRow:
    calls = plan_calls(
        strategy,
        judge_model=_JUDGE_MODEL,
        prompt_tokens=INPUT_TOKENS_PER_CALL - PROMPT_OVERHEAD_TOKENS,
        include_escalation=escalate,
    )
    forecast = forecast_calls(calls, catalog.models, pricing)
    return CostRow(strategy.name, len(calls), forecast.usd)


def _priced(strategy: Strategy, catalog: Catalog) -> bool:
    """Only strategies made of paid cloud models belong in a cost comparison."""
    return all(
        alias in catalog.models and catalog.models[alias].provider not in FREE_PROVIDERS
        for alias in strategy.models
    )


def cost_rows(catalog: Catalog, book: StrategyBook, *, on: date) -> list[CostRow]:
    """One row per paid strategy (two for a cascade: it stops early, or it escalates)."""
    pricing = PricingRegistry(catalog, today=on)
    rows: list[CostRow] = []
    for name in book.names():
        strategy = book.get(name)
        if not _priced(strategy, catalog):
            continue
        if strategy.kind == "cascade":
            stop = _cost(strategy, catalog, pricing, escalate=False)
            rows.append(CostRow(f"{name} (first wave agrees)", stop.calls, stop.usd))
            full = _cost(strategy, catalog, pricing, escalate=True)
            rows.append(CostRow(f"{name} (escalates)", full.calls, full.usd))
        else:
            rows.append(_cost(strategy, catalog, pricing, escalate=True))
    return sorted(rows, key=lambda r: (r.usd, r.label))


def render_cost_table(catalog: Catalog, book: StrategyBook, *, on: date) -> str:
    """The Markdown table, cheapest strategy first."""
    rows = cost_rows(catalog, book, on=on)
    reference = next((r.usd for r in rows if r.label == REFERENCE), None)
    lines = [
        f"| Strategy | Calls | Cost per task | Relative to `{REFERENCE}` |",
        "|----------|-------|---------------|----------------------|",
    ]
    for row in rows:
        relative = f"{row.usd / reference:.2f}x" if reference else "n/a"
        lines.append(f"| `{row.label}` | {row.calls} | ${row.usd:.4f} | {relative} |")
    return "\n".join(lines)


def table_date(document: str) -> date | None:
    """The date the table in ``document`` was generated for."""
    found = _START.search(document)
    return date.fromisoformat(found.group(1)) if found else None


def update_document(document: str, table: str, *, on: date) -> str:
    """``document`` with the table between its markers replaced (and the marker date updated)."""
    if not _BLOCK.search(document):
        msg = "the document has no <!-- cost-table:start ... --> / <!-- cost-table:end --> block"
        raise ValueError(msg)
    block = f"<!-- cost-table:start as-of={on.isoformat()} -->\n{table}\n<!-- cost-table:end -->"
    return _BLOCK.sub(lambda _m: block, document, count=1)

