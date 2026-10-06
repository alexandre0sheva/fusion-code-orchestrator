"""Synthetic benchmark items for the statistics and report tests: measured numbers are chosen, not
simulated, so a test knows the answer."""

from __future__ import annotations

import random
from typing import Any

from fusion.bench.metrics import BenchMetrics
from fusion.bench.store import BenchItem


def item(
    arm: str,
    task: str,
    quality: float | None,
    *,
    repeat: int = 1,
    cost: float = 0.01,
    seconds: float = 10.0,
    category: str = "code_review",
    solved: bool | None = None,
    status: str = "completed",
    **metrics: Any,
) -> BenchItem:
    values: dict[str, Any] = {
        "seconds_to_complete": seconds,
        "cost_usd": cost,
        "cost_known": True,
        "input_tokens": 1000,
        "output_tokens": 500,
        "reasoning_tokens": 0,
        "output_tokens_per_s": 500 / seconds,
        "decode_tokens_per_s": {},
        "ttft_ms": {},
        "calls": 3,
        "retries": 0,
        "critical_path_ms": seconds * 1000,
        "quality": quality,
        "solved": (quality is not None and quality >= 0.6) if solved is None else solved,
    }
    values.update(metrics)
    return BenchItem(
        run_id="r",
        job_key=f"{arm}/{task}/{repeat}",
        task_id=task,
        category=category,
        arm=arm,
        repeat=repeat,
        seed=repeat,
        status=status,  # type: ignore[arg-type]
        metrics=BenchMetrics(**values),
    )


def two_arms(
    tasks: int = 40,
    *,
    effect: float = 0.0,
    noise: float = 0.1,
    repeats: int = 2,
    seed: int = 1,
    cost: tuple[float, float] = (0.01, 0.02),
    seconds: tuple[float, float] = (5.0, 10.0),
    category: str = "code_review",
    prefix: str = "t",
) -> list[BenchItem]:
    """Arm ``fusion`` is ``effect`` better than arm ``solo`` on every task, plus noise."""
    rng = random.Random(seed)  # noqa: S311
    items: list[BenchItem] = []
    for n in range(tasks):
        base = rng.uniform(0.3, 0.8)  # how hard the task is: shared by both arms
        for repeat in range(1, repeats + 1):
            for arm, shift, usd, secs in (
                ("solo", 0.0, cost[1], seconds[1]),
                ("fusion", effect, cost[0], seconds[0]),
            ):
                q = min(1.0, max(0.0, base + shift + rng.gauss(0, noise)))
                items.append(
                    item(
                        arm,
                        f"{prefix}{n:03d}",
                        q,
                        repeat=repeat,
                        cost=usd,
                        seconds=secs,
                        category=category,
                    )
                )
    return items
