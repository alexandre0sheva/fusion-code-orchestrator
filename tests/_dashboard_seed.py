"""A realistic run database for the dashboard: tests and screenshots seed it the same way.

The runs are written through ``RunStore`` (as the pipelines write them), then dated back over a few
weeks. Each stored raw input holds ``RAW_SECRET``, which must never come out of the dashboard.
"""

from __future__ import annotations

import random
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fusion.storage.run_store import RunStepRecord, RunStore

RAW_SECRET = "sk-raw-secret-DO-NOT-LEAK-0123456789"
TASKS = ["code_review", "debug", "architecture", "plan", "default"]
STRATEGIES = [("panel-duo", 0.5), ("panel-cheap", 0.2), ("solo-cheap", 0.2), ("panel-cascade", 0.1)]
MODELS = ["claude-haiku", "gpt-luna", "gemini-flash", "claude-sonnet"]
QUESTIONS = [
    "Why does this retry loop never back off after a 429?",
    "Review the connection pool change before merge",
    "Redis or Postgres for a job queue with ten workers?",
    "Plan rate limiting per API key with a sliding window",
    "Why is the nightly export twice as slow since the ORM upgrade?",
]


def _calls(rng: random.Random, strategy: str, panel: list[str]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    clock = 4.0
    for model in panel:
        latency = rng.uniform(1800, 6200)
        out_tokens = rng.randint(220, 900)
        calls.append(_call("panel", model, clock + rng.uniform(0, 30), latency, out_tokens, rng))
    clock = max(c["started_at_ms"] + c["latency_ms"] for c in calls) + 8
    if strategy == "panel-cascade" and rng.random() < 0.5:
        calls.append(_call("panel", "claude-sonnet", clock, rng.uniform(2500, 5000), 700, rng))
        clock = calls[-1]["started_at_ms"] + calls[-1]["latency_ms"] + 8
    if len(panel) > 1:
        judge = _call("judge", "claude-haiku", clock, rng.uniform(900, 1700), 160, rng)
        synth = _call("synthesis", "claude-haiku", clock + 12, rng.uniform(1400, 2600), 520, rng)
        calls += [judge, synth]
    if rng.random() < 0.08:
        calls[0].update(ok=False, status="timeout", error="TimeoutError: Timed out after 30.0s")
    return calls


def _call(
    stage: str, model: str, start: float, latency: float, out_tokens: int, rng: random.Random
) -> dict[str, Any]:
    in_tokens = rng.randint(900, 3200)
    price = {"claude-haiku": (1.0, 5.0), "gpt-luna": (0.3, 1.5), "claude-sonnet": (3.0, 15.0)}.get(
        model, (0.1, 0.4)
    )
    cost = in_tokens * price[0] / 1e6 + out_tokens * price[1] / 1e6
    return {
        "stage": stage,
        "model_alias": model,
        "provider": "seed",
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "cost_usd": cost,
        "cost_known": True,
        "latency_ms": latency,
        "started_at_ms": start,
        "ok": True,
        "status": "success",
        "error": None,
        "ttft_ms": rng.uniform(300, 900),
        "output_tokens_per_s": out_tokens / (latency / 1000),
        "retries": 0,
        "cache_hit": False,
    }


def seed_runs(db_path: Path, *, runs: int = 48, days: int = 18, seed: int = 7) -> list[str]:
    """Write ``runs`` completed runs spread over ``days`` days; returns their ids, newest first."""
    rng = random.Random(seed)
    store = RunStore(db_path=str(db_path))
    ids: list[str] = []
    stamps: dict[str, str] = {}
    now = datetime.now(UTC)
    names, weights = zip(*STRATEGIES, strict=True)
    for index in range(runs):
        strategy = rng.choices(names, weights)[0]
        task = rng.choice(TASKS)
        panel = {"solo-cheap": MODELS[:1], "panel-cheap": MODELS[:3]}.get(strategy, MODELS[:2])
        calls = _calls(rng, strategy, panel)
        cost = sum(c["cost_usd"] for c in calls)
        baseline = cost * rng.uniform(5.5, 11.0)
        wall = max(c["started_at_ms"] + c["latency_ms"] for c in calls)
        question = rng.choice(QUESTIONS)
        redactions = rng.choice([0, 0, 0, 1, 2])
        n_models = len(panel)
        claims = [
            {
                "id": f"C{n}",
                "kind": rng.choice(["finding", "recommendation", "risk"]),
                "text": text,
                "severity": rng.choice(["high", "med", "low"]),
                "file": "src/http/client.py" if n == 1 else None,
                "line": 88 if n == 1 else None,
                "models": panel[: rng.randint(1, n_models)],
                "members": [{"model": panel[0], "claim": {"evidence": "backoff = base * 2 ** n"}}],
            }
            for n, text in enumerate(
                [
                    "The retry loop sleeps a constant interval, so a 429 storm continues",
                    "Add jitter so synchronized clients do not retry in lockstep",
                    "Honour the Retry-After header before the next attempt",
                    "Cap the total retry time, not only the attempt count",
                ],
                start=1,
            )
        ]
        run_id = store.create_run(
            task_type=task,
            input_data={"primary_content": f"{question}\nkey={RAW_SECRET}", "context": RAW_SECRET},
            sanitized_input={
                "primary_content": f"{question}\nkey=[REDACTED]",
                "context": "",
                "file_snippets": [],
                "changed_files": [],
                "redaction_count": redactions,
            },
        )
        output = {
            "final_answer": "Back off exponentially with jitter.",
            "display_markdown": (
                "## Fusion Answer\n\n### Answer\nBack off exponentially, add jitter, and honour "
                "`Retry-After`.\n\n### Key claims\n- [high] The loop sleeps a constant interval\n"
                "- [med] Add jitter\n\n### Confidence\n0.62\n"
            ),
            "agreement": {
                "n_models": n_models, "n_requested": n_models, "n_clusters": len(claims),
                "score": 0.6, "confidence": round(rng.uniform(0.3, 0.9), 2),
                "evidence_rate": 0.5, "coverage": 1.0, "low_information": False,
                "consensus": ["C1", "C2"] if n_models > 1 else [], "unique": ["C3"],
                "contradicted": ["C4"] if rng.random() < 0.2 else [], "outliers": [],
            },
            "claims": claims,
            "usage": {"successful_model_calls": sum(c["ok"] for c in calls)},
            "cost_comparison": {
                "baseline_name": "Opus 5.5",
                "baseline_estimated_cost_usd": baseline,
                "fusion_total_cost_usd": cost,
            },
            "ledger": {"calls": calls, "by_stage": _by_stage(calls)},
            "task_metrics": {"seconds_to_complete": wall / 1000, "calls": len(calls)},
        }
        store.complete_run(
            run_id,
            status="completed" if rng.random() > 0.05 else "failed",
            output_data=output,
            trace={},
            total_cost_usd=cost,
            total_latency_ms=wall,
            steps=[
                RunStepRecord(
                    step_name=c["stage"], model_name=c["model_alias"], cost_usd=c["cost_usd"],
                    latency_ms=c["latency_ms"], input_tokens=c["input_tokens"],
                    output_tokens=c["output_tokens"],
                )
                for c in calls
            ],
            routing={"strategy": strategy, "selected_panel": panel, "judge_model": "claude-haiku"},
            warnings=["claude-haiku: slow response"] if rng.random() < 0.1 else [],
        )
        when = now - timedelta(days=days * (1 - index / runs), minutes=rng.randint(0, 600))
        stamps[run_id] = when.strftime("%Y-%m-%d %H:%M:%S")
        ids.append(run_id)
        if rng.random() < 0.35:
            from fusion.storage.run_store import ShadowComparisonRecord

            store.record_shadow_comparison(
                ShadowComparisonRecord(
                    run_id=run_id, baseline_model="claude-opus", task_type=task,
                    winner=rng.choices(["fusion", "baseline", "tie"], [5, 3, 2])[0],
                    fusion_score=round(rng.uniform(0.5, 0.9), 2),
                    baseline_score=round(rng.uniform(0.55, 0.9), 2),
                    fusion_cost_usd=cost, baseline_cost_usd=baseline,
                )
            )
    store.close()
    with closing(sqlite3.connect(db_path)) as conn:
        for run_id, stamp in stamps.items():
            conn.execute("UPDATE runs SET created_at = ? WHERE run_id = ?", (stamp, run_id))
    return list(reversed(ids))


def _by_stage(calls: list[dict[str, Any]]) -> dict[str, Any]:
    stages: dict[str, dict[str, Any]] = {}
    for c in calls:
        s = stages.setdefault(
            c["stage"],
            {"calls": 0, "ok_calls": 0, "cost_usd": 0.0, "cost_known": True,
             "input_tokens": 0, "output_tokens": 0, "latency_ms": 0.0},
        )
        s["calls"] += 1
        s["ok_calls"] += int(c["ok"])
        s["cost_usd"] += c["cost_usd"]
        s["input_tokens"] += c["input_tokens"]
        s["output_tokens"] += c["output_tokens"]
        s["latency_ms"] += c["latency_ms"]
    return stages
