"""Helpers for benchmark tests: tiny datasets and simulated environments, all offline."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from fusion.bench.arms import parse_arms
from fusion.bench.runner import BenchEnv, build_env
from fusion.bench.spec import BenchConfig, BenchTask, load_dataset
from fusion.bench.spend import SpendLedger


def point(id: str, *keywords: str, text: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": id, "keywords": list(keywords)}
    if text:
        out["text"] = text
    return out


def write_dataset(path: Path, count: int = 4) -> Path:
    """``count`` distinct code-review tasks, each with three points and one decoy."""
    rows = []
    for n in range(count):
        rows.append(
            {
                "id": f"t{n}",
                "category": "code_review",
                "difficulty": "medium",
                "prompt": f"Review change number {n} to the billing module before release.",
                "context": "A Python service that charges customers once a month.",
                "files": {f"billing/m{n}.py": "def charge(c):\n    return c.total * 1.0\n"},
                "truth": {
                    "points": [
                        point(f"p{n}a", f"alpha-{n}"),
                        point(f"p{n}b", f"bravo-{n}"),
                        point(f"p{n}c", f"charlie-{n}"),
                    ],
                    "decoys": [point(f"d{n}", f"wrong-{n}")],
                },
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def config(dataset: Path, arms: str = "solo-cheap,panel-cheap", **kw: Any) -> BenchConfig:
    values: dict[str, Any] = {
        "dataset": dataset,
        "arms": parse_arms(arms),
        "repeats": 2,
        "max_usd": 100.0,
        "mock": True,
    }
    values.update(kw)
    return BenchConfig(**values)


def sim_env(
    cfg: BenchConfig, tmp: Path, *, spend: SpendLedger | None = None, **changes: Any
) -> BenchEnv:
    """A simulated environment writing under ``tmp``; ``spend`` makes it behave as a live one."""
    tasks: list[BenchTask] = load_dataset(cfg.dataset)
    env = build_env(cfg.model_copy(update={"mock": True}), tasks, results_dir=tmp)
    return dataclasses.replace(env, spend=spend, **changes)


def worst_job_usd(cfg: BenchConfig, env: BenchEnv) -> float:
    """The most any single job of ``cfg`` is expected to cost, by the planner's worst case."""
    from fusion.bench.arms import arm_book
    from fusion.bench.plan import estimate_job
    from fusion.bench.scoring import get_scorer
    from fusion.routing.policy import RoutingPolicy

    book = arm_book(env.book, cfg.arms)
    routing = RoutingPolicy(env.routing_config, registry=env.registry, strategies=book)
    return max(
        estimate_job(
            task,
            book.get(arm.name),
            routing=routing,
            registry=env.registry,
            pricing=env.pricing,
            scorer=get_scorer(task.category),
        ).worst_usd
        for task in load_dataset(cfg.dataset)
        for arm in cfg.arms
    )
