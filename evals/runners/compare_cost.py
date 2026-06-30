#!/usr/bin/env python3
"""Compare Fusion orchestration cost vs Claude Opus (or other) token usage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fusion.storage.run_store import RunStore
from fusion.telemetry.pricing import MODEL_PRICING, estimate_cost_usd


def _load_fusion_usage(run_id: str, db_path: str | None) -> dict[str, object]:
    store = RunStore(db_path=db_path)
    record = store.get_run(run_id)
    if record is None:
        msg = f"Fusion run not found: {run_id}"
        raise SystemExit(msg)

    input_tokens = sum(s.input_tokens for s in record.steps)
    output_tokens = sum(s.output_tokens for s in record.steps)
    return {
        "run_id": record.run_id,
        "task_type": record.task_type,
        "total_cost_usd": record.total_cost_usd,
        "total_latency_ms": record.total_latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "steps": [
            {
                "step": s.step_name,
                "model": s.model_name,
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
                "cost_usd": s.cost_usd,
            }
            for s in record.steps
        ],
    }


def _print_row(label: str, tokens_in: int, tokens_out: int, cost: float, latency_ms: float) -> None:
    print(
        f"{label:<12} "
        f"in={tokens_in:>7,} out={tokens_out:>7,} "
        f"cost=${cost:>8.4f} latency={latency_ms:>8.0f}ms"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare Fusion run cost vs Claude Code Opus token usage"
    )
    parser.add_argument("--fusion-run-id", required=True, help="Fusion run_id from MCP/CLI output")
    parser.add_argument("--opus-input-tokens", type=int, required=True)
    parser.add_argument("--opus-output-tokens", type=int, required=True)
    parser.add_argument(
        "--opus-model",
        default="claude-opus-4-20250514",
        choices=sorted(MODEL_PRICING.keys()),
        help="Model used for the Opus baseline answer",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Fusion SQLite path (default: FUSION_DB_PATH)",
    )
    parser.add_argument(
        "--fusion-eval-json",
        type=Path,
        help="Optional fusion_eval_answer JSON; run_id read if --fusion-run-id omitted",
    )
    args = parser.parse_args()

    run_id = args.fusion_run_id
    if args.fusion_eval_json:
        data = json.loads(args.fusion_eval_json.read_text(encoding="utf-8"))
        run_id = str(data.get("run_id", run_id))

    fusion = _load_fusion_usage(run_id, args.db_path)
    opus_cost = estimate_cost_usd(
        input_tokens=args.opus_input_tokens,
        output_tokens=args.opus_output_tokens,
        model=args.opus_model,
    )

    print("Cost comparison (list prices, approximate)")
    print("-" * 72)
    _print_row(
        "Fusion",
        int(fusion["input_tokens"]),
        int(fusion["output_tokens"]),
        float(fusion["total_cost_usd"]),
        float(fusion["total_latency_ms"]),
    )
    _print_row(
        "Opus",
        args.opus_input_tokens,
        args.opus_output_tokens,
        opus_cost,
        0.0,
    )
    print("-" * 72)

    delta = float(fusion["total_cost_usd"]) - opus_cost
    if delta < 0:
        print(f"Fusion was ${abs(delta):.4f} cheaper than estimated Opus for this task.")
    elif delta > 0:
        print(f"Fusion was ${delta:.4f} more expensive than estimated Opus for this task.")
    else:
        print("Estimated costs are equal.")

    print()
    print("Notes:")
    print("- Fusion cost = panel + synthesis API calls (judge calls may add extra cost).")
    print("- Opus tokens come from Claude Code session stats, not Fusion MCP.")
    print(
        "- Fusion runs multiple models; Opus is one call — compare quality too, not just price."
    )
    print()
    print("Fusion step breakdown:")
    for step in fusion["steps"]:
        if not step["model"]:
            continue
        print(
            f"  {step['step']}: {step['model']} "
            f"in={step['input_tokens']} out={step['output_tokens']} "
            f"${step['cost_usd']:.4f}"
        )


if __name__ == "__main__":
    main()
