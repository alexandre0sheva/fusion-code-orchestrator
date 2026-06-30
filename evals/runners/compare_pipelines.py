#!/usr/bin/env python3
"""Compare pipeline results across task types."""

from __future__ import annotations

import argparse
import asyncio
import os

from fusion.config.env import load_env
from fusion.orchestration.pipelines import PipelineContext, create_pipeline
from fusion.routing.classifier import TaskType

SAMPLES = {
    TaskType.CODE_REVIEW: "+ def foo(): pass",
    TaskType.DEBUGGING: "Error: timeout in connection pool",
    TaskType.ARCHITECTURE: "Should we use event sourcing?",
    TaskType.PLANNING: "Implement user notifications",
}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Compare pipeline task scores")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use mock providers (no API keys required)",
    )
    args = parser.parse_args()

    load_env()
    if args.mock:
        os.environ["FUSION_DEFAULT_PROVIDER"] = "mock"

    pipeline = create_pipeline(use_mock=args.mock)
    print(f"{'Task':<15} {'Score':>8} {'Latency':>10} {'Cost':>8}")
    print("-" * 45)
    for task_type, content in SAMPLES.items():
        result = await pipeline.run(PipelineContext(task_type=task_type, primary_content=content))
        print(
            f"{task_type.value:<15} {result.final_eval.overall_score:>8.2f} "
            f"{result.total_latency_ms:>8.0f}ms ${result.total_cost_usd:>6.4f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
