"""The spend ledger: every live call a study makes is added here, and nothing may pass the cap.

Each roadmap has its own budget (``BUDGET_CAPS_USD``): $20 for 0.2.0, $15 for 0.3.0.
``bench-results/spend.json`` is a JSON array of ``{"date", "task", "purpose", "usd", "budget"}``
entries that is only ever appended to. An entry without ``budget`` was written by 0.2.0 and belongs
to ``v0.2.0``, so earlier spending never counts against the current budget. Every live entry point
asks ``SpendLedger.check`` before it spends and ``append`` after, so the cap holds across runs,
processes and sessions. Simulated (``--mock``) runs cost nothing and are not recorded.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fusion.config.layers import ConfigError
from fusion.config.paths import bench_results_dir

__all__ = [
    "BUDGET_CAPS_USD",
    "CURRENT_BUDGET",
    "LEGACY_BUDGET",
    "SPEND_FILE",
    "SpendCapError",
    "SpendLedger",
    "default_ledger",
]

# Owner decisions: $20 for the 0.2.0 roadmap, $15 for 0.3.0 (roadmap Part 3, rule 6).
BUDGET_CAPS_USD: dict[str, float] = {"v0.2.0": 20.0, "v0.3.0": 15.0}
CURRENT_BUDGET = "v0.3.0"
LEGACY_BUDGET = "v0.2.0"  # the budget of entries written before entries had a ``budget`` field
SPEND_FILE = "spend.json"


class SpendCapError(ConfigError):
    """A live call would take the cumulative spend past the cap (or the ledger is unreadable)."""


class SpendLedger:
    """Append-only record of live spend, with the cap enforced against one budget's total."""

    def __init__(
        self, path: Path, budget: str = CURRENT_BUDGET, cap_usd: float | None = None
    ) -> None:
        if cap_usd is None:
            if budget not in BUDGET_CAPS_USD:
                msg = (
                    f"No spend cap is defined for budget '{budget}' "
                    f"(known: {', '.join(BUDGET_CAPS_USD)}); pass cap_usd"
                )
                raise SpendCapError(msg)
            cap_usd = BUDGET_CAPS_USD[budget]
        self.path = path
        self.budget = budget
        self.cap_usd = cap_usd
        self._lock = threading.Lock()

    # -- reading ------------------------------------------------------------------------------

    def all_entries(self) -> list[dict[str, Any]]:
        """Every entry of every budget, oldest first. A missing file is an empty ledger; a damaged
        one is an error (guessing would let spending continue past the cap)."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            msg = (
                f"The spend ledger {self.path} is not valid JSON ({exc.msg}); "
                "fix or restore it before any live call"
            )
            raise SpendCapError(msg) from exc
        if not isinstance(data, list):
            msg = f"The spend ledger {self.path} must be a JSON array of entries"
            raise SpendCapError(msg)
        return [e for e in data if isinstance(e, dict)]

    def entries(self) -> list[dict[str, Any]]:
        """This ledger's budget's entries, oldest first."""
        return [e for e in self.all_entries() if _budget_of(e) == self.budget]

    def total(self) -> float:
        return sum(float(e.get("usd", 0.0)) for e in self.entries())

    def budget_summary(self) -> dict[str, tuple[float, float, float]]:
        """``{budget: (spent, cap, remaining)}`` for every known budget and any in the file."""
        spent: dict[str, float] = dict.fromkeys(BUDGET_CAPS_USD, 0.0)
        for entry in self.all_entries():
            name = _budget_of(entry)
            spent[name] = spent.get(name, 0.0) + float(entry.get("usd", 0.0))
        summary: dict[str, tuple[float, float, float]] = {}
        for name, used in spent.items():
            cap = BUDGET_CAPS_USD.get(name)
            summary[name] = (
                (used, cap, max(cap - used, 0.0)) if cap is not None else (used, 0.0, 0.0)
            )
        return summary

    def remaining(self) -> float:
        return max(self.cap_usd - self.total(), 0.0)

    # -- enforcing ----------------------------------------------------------------------------

    def check(self, estimate_usd: float, purpose: str = "") -> None:
        """Raise ``SpendCapError`` unless spending ``estimate_usd`` more stays within the cap."""
        spent = self.total()
        if spent + estimate_usd > self.cap_usd + 1e-9:
            what = f" for {purpose}" if purpose else ""
            msg = (
                f"The ${self.cap_usd:.2f} {self.budget} live-spend cap would be passed: "
                f"${spent:.4f} is "
                f"already spent and the call{what} is estimated at ${estimate_usd:.4f} "
                f"(${max(self.cap_usd - spent, 0.0):.4f} left). See {self.path}"
            )
            raise SpendCapError(msg)

    def append(self, usd: float, *, task: str, purpose: str) -> None:
        """Record ``usd`` of live spend. Zero-cost calls are not recorded."""
        if usd <= 0:
            return
        entry = {
            "date": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "task": task,
            "purpose": purpose,
            "usd": round(usd, 8),
            "budget": self.budget,
        }
        with self._lock:
            entries = self.all_entries()
            entries.append(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entries, indent=1) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)  # a crash leaves the old ledger, never half a new one


def _budget_of(entry: dict[str, Any]) -> str:
    return str(entry.get("budget") or LEGACY_BUDGET)


def default_ledger() -> SpendLedger:
    return SpendLedger(bench_results_dir() / SPEND_FILE)
