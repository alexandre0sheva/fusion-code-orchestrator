"""Helpers for golden (characterization) tests of pipeline outputs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path(__file__).parent / "golden"
_VOLATILE_KEYS = re.compile(
    r"(_ms$|_wall_|^wall|elapsed|started_at|^run_id$|created_at|ttft|per_s$)", re.I
)
_RUN_TAG = re.compile(r"mock:[0-9a-f]{6,}")
_WALL_LINE = re.compile(r"^- (Fusion wall time|Shadow baseline latency): .*$", re.M)


def normalize(value: Any) -> Any:
    """Drop timing/ID fields and scrub volatile text so snapshots are deterministic."""
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in sorted(value.items()) if not _VOLATILE_KEYS.search(k)}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, str):
        text = _WALL_LINE.sub(lambda m: f"- {m.group(1)}: <t>", value)
        return _RUN_TAG.sub("mock:<id>", text)
    return value


def check_golden(name: str, actual: Any) -> None:
    """Compare against ``tests/golden/<name>.json``; set UPDATE_GOLDEN=1 to (re)write it."""
    path = GOLDEN_DIR / f"{name}.json"
    rendered = json.dumps(normalize(actual), indent=2, sort_keys=True, default=str) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.write_text(rendered, encoding="utf-8")
        return
    assert path.exists(), f"missing golden file {path}; run with UPDATE_GOLDEN=1 once"
    assert rendered == path.read_text(encoding="utf-8"), f"golden mismatch for {name}"
