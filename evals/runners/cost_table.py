#!/usr/bin/env python3
"""Print the strategy cost table, or rewrite it inside docs/COSTS.md.

    uv run python evals/runners/cost_table.py            # print for today's prices
    uv run python evals/runners/cost_table.py --write    # update docs/COSTS.md in place
    uv run python evals/runners/cost_table.py --on 2026-10-05
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from fusion.config.catalog import load_catalog
from fusion.orchestration.strategy import load_strategy_book
from fusion.telemetry.cost_table import render_cost_table, update_document

DOC = Path(__file__).resolve().parents[2] / "docs" / "COSTS.md"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--on", type=date.fromisoformat, default=date.today(), help="price date")
    parser.add_argument("--write", action="store_true", help="rewrite the table in docs/COSTS.md")
    args = parser.parse_args()
    table = render_cost_table(load_catalog(), load_strategy_book(), on=args.on)
    if not args.write:
        print(table)
        return
    DOC.write_text(update_document(DOC.read_text(encoding="utf-8"), table, on=args.on), "utf-8")
    print(f"Updated {DOC} for prices on {args.on}")


if __name__ == "__main__":
    main()
