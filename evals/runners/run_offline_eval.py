#!/usr/bin/env python3
"""Run offline evaluations against dataset cases."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from fusion.config.env import load_env
from fusion.orchestration.pipelines import PipelineContext, create_pipeline
from fusion.routing.classifier import TaskType

DATASETS = {
    "code_review": ("code_review_cases.jsonl", TaskType.CODE_REVIEW, "diff"),
    "debugging": ("debugging_cases.jsonl", TaskType.DEBUGGING, "error"),
    "architecture": ("architecture_cases.jsonl", TaskType.ARCHITECTURE, "question"),
    "planning": ("implementation_plan_cases.jsonl", TaskType.PLANNING, "feature"),
}


async def run_case(pipeline, task_type: TaskType, case: dict, content_key: str) -> dict:
    content = case.get(content_key, "")
    if task_type == TaskType.DEBUGGING and case.get("stack_trace"):
        content = f"{content}\n{case['stack_trace']}"
    ctx = PipelineContext(
        task_type=task_type,
        primary_content=content,
        context=case.get("context", ""),
    )
    result = await pipeline.run(ctx)
    return {
        "case_id": case.get("id"),
        "run_id": result.run_id,
        "final_eval_score": result.final_eval.overall_score,
        "latency_ms": result.total_latency_ms,
        "cost_usd": result.total_cost_usd,
        "panel_models": result.routing.selected_panel,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run offline evals")
    parser.add_argument("--dataset", choices=list(DATASETS.keys()), default="code_review")
    parser.add_argument("--db-path", default=":memory:")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use mock providers (no API keys required)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write JSONL results to this file",
    )
    args = parser.parse_args()

    load_env()
    if args.mock:
        os.environ["FUSION_DEFAULT_PROVIDER"] = "mock"

    filename, task_type, content_key = DATASETS[args.dataset]
    dataset_path = Path(__file__).parent.parent / "datasets" / filename
    cases = [json.loads(line) for line in dataset_path.read_text().strip().split("\n") if line]

    pipeline = create_pipeline(
        db_path=args.db_path if args.db_path != ":memory:" else None,
        use_mock=args.mock,
    )
    results = []
    for case in cases:
        r = await run_case(pipeline, task_type, case, content_key)
        results.append(r)
        print(
            f"  {r['case_id']}: score={r['final_eval_score']:.2f} "
            f"latency={r['latency_ms']:.0f}ms cost=${r['cost_usd']:.4f}"
        )

    avg_score = sum(r["final_eval_score"] for r in results) / len(results) if results else 0
    avg_latency = sum(r["latency_ms"] for r in results) / len(results) if results else 0
    total_cost = sum(r["cost_usd"] for r in results)
    print(
        f"\nAverage final eval score: {avg_score:.2f} ({len(results)} cases)\n"
        f"Average latency: {avg_latency:.0f}ms | Total cost: ${total_cost:.4f}"
    )

    if args.output:
        args.output.write_text(
            "\n".join(json.dumps(r) for r in results) + ("\n" if results else ""),
            encoding="utf-8",
        )
        print(f"Wrote results to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
