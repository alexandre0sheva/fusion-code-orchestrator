"""The spend ledger: every live call a study makes is added here, and nothing may pass the cap.

The 0.2.0 roadmap allows $20 of live API spend in total. ``bench-results/spend.json`` is a JSON
array of ``{"date", "task", "purpose", "usd"}`` entries that is only ever appended to. Every live
entry point asks ``SpendLedger.check`` before it spends and ``append`` after, so the cap holds
across runs, processes and sessions. Simulated (``--mock``) runs cost nothing and are not recorded.
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

__all__ = ["LIVE_SPEND_CAP_USD", "SPEND_FILE", "SpendCapError", "SpendLedger", "default_ledger"]

LIVE_SPEND_CAP_USD = 20.0  # owner decision, roadmap Part 5 item 3
SPEND_FILE = "spend.json"


class SpendCapError(ConfigError):
    """A live call would take the cumulative spend past the cap (or the ledger is unreadable)."""


class SpendLedger:
    """Append-only record of live spend, with the cap enforced against its total."""

    def __init__(self, path: Path, cap_usd: float = LIVE_SPEND_CAP_USD) -> None:
        self.path = path
        self.cap_usd = cap_usd
        self._lock = threading.Lock()

    # -- reading ------------------------------------------------------------------------------

    def entries(self) -> list[dict[str, Any]]:
        """Every entry, oldest first. A missing file is an empty ledger; a damaged one is an error
        (guessing would let spending continue past the cap)."""
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

    def total(self) -> float:
        return sum(float(e.get("usd", 0.0)) for e in self.entries())

    def remaining(self) -> float:
        return max(self.cap_usd - self.total(), 0.0)

    # -- enforcing ----------------------------------------------------------------------------

    def check(self, estimate_usd: float, purpose: str = "") -> None:
        """Raise ``SpendCapError`` unless spending ``estimate_usd`` more stays within the cap."""
        spent = self.total()
        if spent + estimate_usd > self.cap_usd + 1e-9:
            what = f" for {purpose}" if purpose else ""
            msg = (
                f"The ${self.cap_usd:.2f} live-spend cap would be passed: ${spent:.4f} is "
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
        }
        with self._lock:
            entries = self.entries()
            entries.append(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entries, indent=1) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)  # a crash leaves the old ledger, never half a new one


def default_ledger() -> SpendLedger:
    return SpendLedger(bench_results_dir() / SPEND_FILE)
