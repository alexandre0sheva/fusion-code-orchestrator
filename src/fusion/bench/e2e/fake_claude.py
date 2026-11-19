"""A stand-in for ``claude`` that replays a recording: for ``fusion bench e2e --mock`` and tests.

``python -m fusion.bench.e2e.fake_claude <claude arguments>`` reads the recording named by
``FUSION_FAKE_CLAUDE`` (a JSON file), and then, like a real run would:

* writes ``writes`` (``{path: content}``) into the working directory, as the agent's edits;
* when ``--mcp-config`` names the Fusion server, records one run per cost in ``fusion_runs`` in the
  database that server would use (its ``FUSION_DB_PATH``), so Fusion's ledger is read as in a real
  run;
* prints ``events`` as JSON lines (Claude Code's stream-json) and exits with ``exit_code``.

``mock_recording`` makes a plausible, seeded recording for one (task, arm): a stronger executor,
and Fusion, solve more often; tokens are priced from the catalog. It is a simulation for checking
the harness and its reports, not a measurement of anything.
"""

from __future__ import annotations

import json
import os
import random
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fusion.bench.e2e.claude_cli import FUSION_SERVER
from fusion.bench.patch import PatchError, apply_patch
from fusion.bench.sandbox import safe_relative_path
from fusion.bench.spec import BenchTask, CodingTruth
from fusion.routing.model_registry import ModelRegistry

if TYPE_CHECKING:
    from fusion.bench.e2e.runner import E2EArm

__all__ = ["RECORDING_ENV", "main", "mock_recording"]

RECORDING_ENV = "FUSION_FAKE_CLAUDE"
# Simulated chance that an executor solves a task, and what Fusion adds. Invented numbers.
_SKILL = {"opus": 0.8, "sonnet": 0.7, "haiku": 0.45}
_FUSION_BONUS = 0.15
_DIFFICULTY = {"easy": 0.1, "medium": 0.0, "hard": -0.15}


def _flag(argv: Sequence[str], name: str) -> str | None:
    for i, arg in enumerate(argv[:-1]):
        if arg == name:
            return argv[i + 1]
    return None


def _fusion_db(argv: Sequence[str]) -> Path | None:
    path = _flag(argv, "--mcp-config")
    if path is None:
        return None
    try:
        servers = json.loads(Path(path).read_text(encoding="utf-8")).get("mcpServers") or {}
    except (OSError, ValueError):
        return None
    db = (servers.get(FUSION_SERVER) or {}).get("env", {}).get("FUSION_DB_PATH")
    return Path(db) if db else None


def _record_fusion_runs(db: Path, costs: list[float]) -> None:
    from fusion.storage.run_store import RunStore

    db.parent.mkdir(parents=True, exist_ok=True)
    store = RunStore(db_path=str(db))
    try:
        for cost in costs:
            run_id = store.create_run(task_type="ask", input_data={}, sanitized_input={})
            store.complete_run(
                run_id,
                status="completed",
                output_data={},
                trace={},
                total_cost_usd=cost,
                total_latency_ms=0.0,
                steps=[],
            )
    finally:
        store.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    source = os.environ.get(RECORDING_ENV)
    if not source:
        print(f"fake claude: {RECORDING_ENV} is not set", file=sys.stderr)
        return 2
    recording: dict[str, Any] = json.loads(Path(source).read_text(encoding="utf-8"))
    if recording.get("argv_log"):
        with Path(recording["argv_log"]).open("a", encoding="utf-8") as log:
            log.write(json.dumps(args) + "\n")
    for rel, content in (recording.get("writes") or {}).items():
        target = Path.cwd() / safe_relative_path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    db = _fusion_db(args)
    if db is not None and recording.get("fusion_runs"):
        _record_fusion_runs(db, [float(c) for c in recording["fusion_runs"]])
    for event in recording.get("events") or []:
        sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()
    return int(recording.get("exit_code", 0))


# -- simulated recordings -------------------------------------------------------------------------


def _edits(task: BenchTask, patch: str) -> dict[str, str]:
    if not patch:
        return {}
    try:
        changes = apply_patch(task.files, patch)
    except PatchError:
        return {}
    return {path: text for path, text in changes.items() if text is not None}


def _tool(name: str, n: int) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": f"tool-{n}", "name": name, "input": {}}]
        },
    }


def mock_recording(
    task: BenchTask, arm: E2EArm, model_id: str, *, seed: int, registry: ModelRegistry
) -> dict[str, Any]:
    """A deterministic simulated run of ``arm`` on ``task`` (see the module docstring)."""
    rng = random.Random(f"e2e-mock:{task.id}:{arm.name}:{seed}")  # noqa: S311 — a simulation
    chance = _SKILL.get(arm.executor, 0.5) + _DIFFICULTY.get(task.difficulty, 0.0)
    if arm.fusion:
        chance += _FUSION_BONUS
    truth = CodingTruth.model_validate(task.truth)
    if rng.random() < min(chance, 0.95):
        writes = _edits(task, truth.solution)
    elif truth.flaws and rng.random() < 0.6:
        writes = _edits(task, rng.choice(truth.flaws).patch)
    else:
        writes = {}

    turns = rng.randint(4, 12)
    fresh, cached, written, output = 1_500 * turns, 22_000 * turns, 2_500 * turns, 500 * turns
    entry = next((m for m in registry.models.values() if m.model_id == model_id), None)
    price = entry.price_at() if entry else None
    cost = 0.0
    if price is not None:
        cost = (
            fresh * price.input_per_1m
            + cached * (price.cached_input_per_1m or price.input_per_1m)
            + written * (price.cache_write_per_1m or price.input_per_1m)
            + output * price.output_per_1m
        ) / 1_000_000
    fusion_runs = (
        [round(rng.uniform(0.002, 0.02), 6) for _ in range(rng.randint(1, 3))] if arm.fusion else []
    )

    tools = ["Read", "Grep", *(["Edit"] if writes else []), "Bash", "Bash"]
    tools += ["mcp__fusion__fusion_ask"] * len(fusion_runs)
    servers = [{"name": FUSION_SERVER, "status": "connected"}] if arm.fusion else []
    events: list[dict[str, Any]] = [
        {"type": "system", "subtype": "init", "model": model_id, "mcp_servers": servers},
        *(_tool(name, n) for n, name in enumerate(tools)),
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": round(turns * rng.uniform(3_000, 8_000) + 12_000 * len(fusion_runs)),
            "num_turns": turns,
            "result": "Simulated run: " + ("edited the code." if writes else "made no change."),
            "session_id": f"mock-{task.id}-{arm.name}-{seed}",
            "total_cost_usd": round(cost, 6),
            "usage": {
                "input_tokens": fresh,
                "cache_read_input_tokens": cached,
                "cache_creation_input_tokens": written,
                "output_tokens": output,
            },
            "modelUsage": {
                model_id: {
                    "inputTokens": fresh,
                    "cacheReadInputTokens": cached,
                    "cacheCreationInputTokens": written,
                    "outputTokens": output,
                    "costUSD": round(cost, 6),
                }
            },
            "permission_denials": [],
        },
    ]
    return {"events": events, "writes": writes, "fusion_runs": fusion_runs}


if __name__ == "__main__":
    raise SystemExit(main())
