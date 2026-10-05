"""The strategy cost table in docs/COSTS.md is generated from the catalog."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from fusion.config.catalog import PriceSchedule, load_catalog
from fusion.orchestration.strategy import load_strategy_book
from fusion.telemetry.cost_table import (
    cost_rows,
    render_cost_table,
    table_date,
    update_document,
)

DOC = Path(__file__).resolve().parents[1] / "docs" / "COSTS.md"
ON = date(2026, 10, 5)


def _block(text: str) -> str:
    start = text.index("<!-- cost-table:start")
    end = text.index("<!-- cost-table:end -->")
    return text[text.index("\n", start) + 1 : end].rstrip("\n")


def test_the_table_in_the_doc_matches_the_catalog_and_packaged_strategies() -> None:
    text = DOC.read_text(encoding="utf-8")
    on = table_date(text)
    assert on is not None, "docs/COSTS.md needs a <!-- cost-table:start as-of=YYYY-MM-DD --> block"
    expected = render_cost_table(load_catalog(), load_strategy_book(), on=on)
    assert _block(text) == expected, (
        "docs/COSTS.md is out of date; run `uv run python evals/runners/cost_table.py --write`"
    )


def test_rows_cover_paid_strategies_and_split_a_cascade_into_its_two_outcomes() -> None:
    rows = {r.label: r for r in cost_rows(load_catalog(), load_strategy_book(), on=ON)}
    assert "panel-local" not in rows  # free models are not a cost comparison
    assert {"solo-frontier", "panel-cheap", "panel-digest", "panel-vote"} <= set(rows)
    early = rows["panel-cascade (first wave agrees)"]
    escalated = rows["panel-cascade (escalates)"]
    assert (early.calls, escalated.calls) == (2, 4)
    assert early.usd < rows["panel-cheap"].usd < escalated.usd


def test_the_arithmetic_for_one_row_is_checkable_by_hand() -> None:
    # solo-frontier: one call, 5,000 tokens in and 1,500 out on Opus 5.5 ($4 / $20 per 1M).
    rows = {r.label: r for r in cost_rows(load_catalog(), load_strategy_book(), on=ON)}
    assert rows["solo-frontier"].usd == pytest.approx(5_000 * 4 / 1e6 + 1_500 * 20 / 1e6)


def test_a_synthesis_free_panel_is_cheaper_than_one_that_synthesizes() -> None:
    rows = {r.label: r for r in cost_rows(load_catalog(), load_strategy_book(), on=ON)}
    assert rows["panel-digest"].usd < rows["panel-cheap"].usd < rows["panel-cheap-strong-synth"].usd


def test_the_table_follows_the_date_when_a_price_changes() -> None:
    catalog = load_catalog()
    book = load_strategy_book()
    before = {r.label: r.usd for r in cost_rows(catalog, book, on=date(2026, 12, 31))}
    after = {r.label: r.usd for r in cost_rows(catalog, book, on=date(2027, 1, 1))}
    assert after["panel-digest"] > before["panel-digest"]  # Gemini's introductory price ends
    assert after["solo-frontier"] == before["solo-frontier"]


def test_prices_edited_in_the_catalog_show_up_in_the_table() -> None:
    catalog = load_catalog()
    opus = catalog.models["claude-opus"]
    doubled = opus.model_copy(
        update={"prices": [PriceSchedule(input_per_1m=8.0, output_per_1m=40.0)]}
    )
    edited = catalog.model_copy(update={"models": {**catalog.models, "claude-opus": doubled}})
    book = load_strategy_book()
    base = {r.label: r.usd for r in cost_rows(catalog, book, on=ON)}
    new = {r.label: r.usd for r in cost_rows(edited, book, on=ON)}
    assert new["solo-frontier"] == pytest.approx(2 * base["solo-frontier"])
    table = render_cost_table(edited, book, on=ON)
    assert "| `solo-frontier` | 1 |" in table and "1.00x" in table


def test_update_document_replaces_only_the_block_and_stamps_the_date() -> None:
    doc = "intro\n<!-- cost-table:start as-of=2026-01-01 -->\nold\n<!-- cost-table:end -->\noutro\n"
    updated = update_document(doc, "NEW TABLE", on=ON)
    assert updated == (
        "intro\n<!-- cost-table:start as-of=2026-10-05 -->\nNEW TABLE\n"
        "<!-- cost-table:end -->\noutro\n"
    )
    assert table_date(updated) == ON


def test_update_document_needs_the_markers() -> None:
    with pytest.raises(ValueError, match="cost-table:start"):
        update_document("no markers here", "x", on=ON)
