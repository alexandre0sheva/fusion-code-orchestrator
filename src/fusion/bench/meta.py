"""What a run was, written down when it started: the facts a report's methodology footer needs.

The dataset, the catalog and the strategies can all change after a run, so the report cannot read
them later and call that the study. ``meta.json`` beside ``results.jsonl`` is the snapshot taken
when the run was created. A run from before it existed gets ``reconstruct_meta``: the same facts
read from today's files, marked ``reconstructed`` so the report says they may differ.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from fusion import __version__
from fusion.bench.arms import arm_book
from fusion.bench.spec import BenchConfig, BenchTask, load_dataset, select_tasks, task_hash
from fusion.config.catalog import ModelEntry
from fusion.orchestration.strategy import StrategyBook, load_strategy_book

__all__ = [
    "META_FILE",
    "ArmMeta",
    "ModelMeta",
    "RunMeta",
    "TaskMeta",
    "build_meta",
    "dataset_hash",
    "load_meta",
    "reconstruct_meta",
    "write_meta",
]

META_FILE = "meta.json"


class TaskMeta(BaseModel):
    category: str
    difficulty: str


class ModelMeta(BaseModel):
    """A catalog model as the run priced it."""

    alias: str
    provider: str
    model_id: str
    input_per_1m: float | None = None
    output_per_1m: float | None = None
    verified_on: str | None = None  # the date the price was last checked against its source
    source_url: str = ""


class ArmMeta(BaseModel):
    name: str
    strategy: str
    overrides: dict[str, object] = Field(default_factory=dict)
    kind: str = ""
    members: list[str] = Field(default_factory=list)
    aggregator: str = ""
    aggregator_model: str | None = None
    rounds: int = 1
    judge: str = "off"


class RunMeta(BaseModel):
    version: str
    created_at: str
    reconstructed: bool = False  # read from today's files, not captured when the run started
    dataset: str
    dataset_hash: str  # of the tasks the run used (after split and limit)
    split: str
    task_count: int
    tasks: dict[str, TaskMeta] = Field(default_factory=dict)
    arms: list[ArmMeta] = Field(default_factory=list)
    models: dict[str, ModelMeta] = Field(default_factory=dict)  # every model an arm or judge uses


def dataset_hash(tasks: list[BenchTask]) -> str:
    """One fingerprint of a set of tasks (order does not matter), from each task's own."""
    digest = hashlib.sha256()
    for key in sorted(f"{t.id}:{task_hash(t)}" for t in tasks):
        digest.update(key.encode())
    return digest.hexdigest()[:16]


def _model_meta(alias: str, entry: ModelEntry) -> ModelMeta:
    price = entry.price_at()
    return ModelMeta(
        alias=alias,
        provider=entry.provider,
        model_id=entry.model_id,
        input_per_1m=price.input_per_1m if price else None,
        output_per_1m=price.output_per_1m if price else None,
        verified_on=price.verified_on.isoformat() if price and price.verified_on else None,
        source_url=price.source_url if price else "",
    )


def build_meta(
    cfg: BenchConfig,
    tasks: list[BenchTask],
    *,
    book: StrategyBook,
    models: dict[str, ModelEntry],
    reconstructed: bool = False,
) -> RunMeta:
    """The snapshot for ``cfg`` run over ``tasks``; ``book`` already holds the arms by name."""
    arms: list[ArmMeta] = []
    used: list[str] = []
    for arm in cfg.arms:
        strategy = book.get(arm.name)
        arms.append(
            ArmMeta(
                name=arm.name,
                strategy=arm.strategy,
                overrides=dict(arm.overrides),
                kind=strategy.kind,
                members=[m.model for m in strategy.members],
                aggregator=strategy.aggregator,
                aggregator_model=strategy.aggregator_model,
                rounds=strategy.rounds,
                judge=strategy.judge,
            )
        )
        used += [m.model for m in strategy.members]
        if strategy.aggregator_model:
            used.append(strategy.aggregator_model)
    used += cfg.judge_models
    return RunMeta(
        version=__version__,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        reconstructed=reconstructed,
        dataset=str(cfg.dataset),
        dataset_hash=dataset_hash(tasks),
        split=cfg.split,
        task_count=len(tasks),
        tasks={t.id: TaskMeta(category=t.category, difficulty=t.difficulty) for t in tasks},
        arms=arms,
        models={a: _model_meta(a, models[a]) for a in dict.fromkeys(used) if a in models},
    )


def write_meta(run_dir: Path, meta: RunMeta) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / META_FILE).write_text(meta.model_dump_json(indent=1), encoding="utf-8")


def load_meta(run_dir: Path) -> RunMeta | None:
    path = run_dir / META_FILE
    if not path.is_file():
        return None
    try:
        return RunMeta.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except ValueError:
        return None


def reconstruct_meta(cfg: BenchConfig, models: dict[str, ModelEntry]) -> RunMeta | None:
    """The run's facts from today's dataset, strategies and catalog, for a run with no snapshot.
    ``None`` when the dataset is no longer where the run found it."""
    try:
        tasks = select_tasks(load_dataset(cfg.dataset, cfg.split), cfg.limit, cfg.seed, cfg.quota)
        book = arm_book(load_strategy_book(), cfg.arms)
        return build_meta(cfg, tasks, book=book, models=models, reconstructed=True)
    except Exception:  # noqa: BLE001 — a missing dataset or a renamed strategy: report without it
        return None
