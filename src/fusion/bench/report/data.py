"""What a report says, as data: the statistics, the caveats, the evidence and the footer facts.

The Markdown and HTML renderers only lay this out; ``--format json`` prints it as it is. Anything
a reader needs to judge the numbers (a simulated run, a partial run, costs that are a lower
bound, a blocked verdict) is a ``note`` here, so no renderer can leave it out.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from fusion.bench.meta import RunMeta
from fusion.bench.scoring.calibration import JudgeGate
from fusion.bench.stats import Rules, StudyStats, build_cells, study_stats
from fusion.bench.store import BenchItem, BenchRunRecord

__all__ = [
    "ARTIFACT_CATEGORIES",
    "EvidenceLine",
    "Example",
    "ExampleSide",
    "Report",
    "RunSummary",
    "Span",
    "build_report",
]

ARTIFACT_CATEGORIES = ("frontend", "performance")  # scored on what the answer builds
MAX_EXAMPLES = 3


class RunSummary(BaseModel):
    run_id: str
    status: str
    stop_reason: str | None = None
    dataset: str
    mock: bool
    repeats: int
    seed: int
    max_usd: float
    total_jobs: int
    done_jobs: int
    spent_usd: float  # what the arms cost
    eval_spent_usd: float  # what scoring and judging cost, on top
    created_at: str
    judge_models: list[str] = Field(default_factory=list)


class EvidenceLine(BaseModel):
    id: str
    kind: str
    status: str
    ok: bool | None
    summary: str = ""
    metrics: dict[str, float] = Field(default_factory=dict)


class ExampleSide(BaseModel):
    arm: str
    repeat: int
    quality: float | None
    gates: list[dict[str, Any]] = Field(default_factory=list)
    criteria: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[EvidenceLine] = Field(default_factory=list)
    screenshots: dict[str, str] = Field(default_factory=dict)  # name -> file path
    justification: str = ""
    eval_cost_usd: float = 0.0
    eval_seconds: float = 0.0


class Example(BaseModel):
    """One task where two arms differed most, with the evidence behind each side's score."""

    task_id: str
    category: str
    gap: float  # challenger quality minus baseline quality (mean over repeats)
    challenger: ExampleSide
    baseline: ExampleSide


class Span(BaseModel):
    stage: str
    model: str
    start_s: float
    end_s: float
    cache_hit: bool = False


class Report(BaseModel):
    run: RunSummary
    stats: StudyStats
    meta: RunMeta | None = None
    judge_gate: JudgeGate | None = None
    examples: list[Example] = Field(default_factory=list)
    timelines: dict[str, list[Span]] = Field(default_factory=dict)  # arm -> calls of a typical run
    timeline_task: dict[str, str] = Field(default_factory=dict)  # arm -> the task shown
    notes: list[str] = Field(default_factory=list)
    spend_total_usd: float | None = None  # the roadmap-wide ledger, when one is available


def _side(item: BenchItem) -> ExampleSide:
    score = item.score
    details = score.details if score else {}
    shots: dict[str, str] = {}
    lines: list[EvidenceLine] = []
    for raw in score.evidence if score else []:
        if raw.get("artifact_path"):
            shots[f"{raw.get('id') or raw.get('kind')}"] = str(raw["artifact_path"])
        for name, path in (raw.get("artifacts") or {}).items():
            shots[f"{raw.get('id') or raw.get('kind')}:{name}"] = str(path)
        lines.append(
            EvidenceLine(
                id=str(raw.get("id") or ""),
                kind=str(raw.get("kind", "")),
                status=str(raw.get("status", "measured")),
                ok=raw.get("ok"),
                summary=str(raw.get("summary", "")),
                metrics={k: float(v) for k, v in (raw.get("metrics") or {}).items()},
            )
        )
    judge = details.get("judge") or {}
    return ExampleSide(
        arm=item.arm,
        repeat=item.repeat,
        quality=item.metrics.quality,
        gates=list(details.get("gates", [])),
        criteria=list(details.get("criteria", [])),
        evidence=lines,
        screenshots=shots,
        justification=str(judge.get("justification", "")),
        eval_cost_usd=item.metrics.eval_cost_usd,
        eval_seconds=item.metrics.eval_seconds,
    )


def _representative(items: list[BenchItem]) -> BenchItem | None:
    """The scored item closest to the task's mean quality (not the luckiest or unluckiest)."""
    scored = [i for i in items if i.metrics.quality is not None]
    if not scored:
        return None
    mean = sum(i.metrics.quality or 0.0 for i in scored) / len(scored)
    return min(scored, key=lambda i: (abs((i.metrics.quality or 0.0) - mean), i.repeat))


def _examples(items: list[BenchItem], stats: StudyStats) -> list[Example]:
    """The tasks with evidence where an arm and the baseline differed most."""
    by_key: dict[tuple[str, str], list[BenchItem]] = defaultdict(list)
    for item in items:
        if item.category in ARTIFACT_CATEGORIES and item.score and item.score.evidence:
            by_key[(item.arm, item.task_id)].append(item)
    cells = build_cells([i for group in by_key.values() for i in group])
    found: list[Example] = []
    base = stats.baseline
    for arm, tasks in cells.items():
        if arm == base:
            continue
        for task_id, cell in tasks.items():
            other = cells.get(base, {}).get(task_id)
            if other is None or cell.quality is None or other.quality is None:
                continue
            a = _representative(by_key[(arm, task_id)])
            b = _representative(by_key[(base, task_id)])
            if a is None or b is None:
                continue
            found.append(
                Example(
                    task_id=task_id,
                    category=cell.category,
                    gap=cell.quality - other.quality,
                    challenger=_side(a),
                    baseline=_side(b),
                )
            )
    found.sort(key=lambda e: (-abs(e.gap), e.task_id, e.challenger.arm))
    return found[:MAX_EXAMPLES]


def _timelines(items: list[BenchItem]) -> tuple[dict[str, list[Span]], dict[str, str]]:
    """For each arm, the calls of the run whose time was the arm's median: a typical run, laid out
    on its own clock (what overlapped, what waited)."""
    by_arm: dict[str, list[BenchItem]] = defaultdict(list)
    for item in items:
        if item.metrics.latency_valid and item.calls:
            by_arm[item.arm].append(item)
    spans: dict[str, list[Span]] = {}
    tasks: dict[str, str] = {}
    for arm, group in by_arm.items():
        ordered = sorted(group, key=lambda i: (i.metrics.seconds_to_complete, i.job_key))
        typical = ordered[(len(ordered) - 1) // 2]
        spans[arm] = [
            Span(
                stage=call.stage,
                model=call.model_alias,
                start_s=call.started_at_ms / 1000,
                end_s=(call.started_at_ms + call.latency_ms) / 1000,
                cache_hit=call.cache_hit,
            )
            for call in sorted(typical.calls, key=lambda c: (c.started_at_ms, c.model_alias))
            if call.stage not in ("shadow_baseline", "shadow_judge")
        ]
        tasks[arm] = typical.task_id
    return spans, tasks


def _notes(
    record: BenchRunRecord,
    items: list[BenchItem],
    stats: StudyStats,
    meta: RunMeta | None,
    gate: JudgeGate | None,
    retried: dict[str, int],
) -> list[str]:
    notes: list[str] = []
    if retried:
        listed = ", ".join(f"{arm} ({n})" for arm, n in retried.items())
        notes.append(
            f"{sum(retried.values())} job(s) halted on their first attempt (a call timed out or "
            f"the panel had no quorum) and were run again, with longer timeouts where it was a "
            f"timeout: {listed}. Only the last attempt is scored; the first attempt's cost is in "
            "the totals. A halted job scores 0, so without the retry these arms would be "
            "understated by the harness's limit, not by their answers."
        )
    if record.mock:
        notes.append(
            "Simulated run: the models are simulated, with skill, speed and mistakes set by "
            "assumption. These numbers test the harness; they say nothing about real models."
        )
    if record.status != "completed":
        notes.append(
            f"The run is {record.status} ({record.done_jobs}/{record.total_jobs} jobs finished"
            + (f"; {record.stop_reason}" if record.stop_reason else "")
            + "): arms may have answered different tasks."
        )
    if meta is None:
        notes.append(
            "No dataset snapshot is available (the dataset moved or changed): the footer's "
            "dataset hash and the difficulty breakdown are missing."
        )
    elif meta.reconstructed:
        notes.append(
            "The footer facts (dataset hash, models, prices) were read from today's files, not "
            "captured when the run started; they may differ from what the run used."
        )
    unknown = [a.arm for a in stats.arms if not a.cost_known]
    if unknown:
        notes.append(
            f"Some prices were unknown for {', '.join(unknown)}: its costs are a lower bound."
        )
    errors = sum(a.errors for a in stats.arms)
    if errors:
        notes.append(
            f"{errors} item(s) ended in an error and have no quality; their cost is counted, "
            "their answers are not. Resume the run to retry them."
        )
    replayed = [a.arm for a in stats.arms if a.timed_items < a.n_items]
    if replayed:
        notes.append(
            f"Items replayed from the response cache are left out of the latency figures "
            f"({', '.join(replayed)}): their wall time is not a measurement."
        )
    repeats = record.config.repeats
    if repeats < 3:
        notes.append(f"{repeats} repeat(s) per task: variance across repeats is poorly estimated.")
    paired = [c.n_tasks for c in stats.comparisons if c.scope == "all"]
    if paired and min(paired) < stats.min_tasks:
        notes.append(
            f"Fewer than {stats.min_tasks} tasks are answered by both an arm and the baseline: "
            "no verdict is possible for it."
        )
    if gate is not None and gate.blocked:
        notes.append(
            "Headline verdict blocked: the judges are not shown to be reliable ("
            + "; ".join(gate.reasons)
            + "). Verdicts on categories they scored are withheld."
        )
    if any(i.category in ARTIFACT_CATEGORIES for i in items):
        notes.append(
            "Frontend and performance scores measure what the answer builds (tests, timings, "
            "screenshots); they do not judge whether it matches the author's intent."
        )
    return notes


def build_report(
    record: BenchRunRecord,
    items: list[BenchItem],
    *,
    meta: RunMeta | None = None,
    gate: JudgeGate | None = None,
    baseline: str | None = None,
    rules: Rules | None = None,
    spend_total_usd: float | None = None,
    retried_after_halt: dict[str, int] | None = None,
) -> Report:
    """Everything a report shows for one run."""
    judged = frozenset(
        i.category
        for i in items
        if i.category in ARTIFACT_CATEGORIES and i.score is not None and i.score.trail
    )
    blocked = frozenset({"all", *judged}) if gate is not None and gate.blocked else frozenset()
    difficulties = {t: m.difficulty for t, m in meta.tasks.items()} if meta else None
    stats = study_stats(
        items,
        difficulties=difficulties,
        baseline=baseline,
        rules=rules,
        blocked_categories=blocked,
    )
    cfg = record.config
    timelines, timeline_task = _timelines(items)
    return Report(
        run=RunSummary(
            run_id=record.run_id,
            status=record.status,
            stop_reason=record.stop_reason,
            dataset=record.dataset,
            mock=record.mock,
            repeats=cfg.repeats,
            seed=cfg.seed,
            max_usd=cfg.max_usd,
            total_jobs=record.total_jobs,
            done_jobs=record.done_jobs,
            spent_usd=record.spent_usd,
            eval_spent_usd=record.eval_spent_usd,
            created_at=record.created_at,
            judge_models=list(cfg.judge_models),
        ),
        stats=stats,
        meta=meta,
        judge_gate=gate,
        examples=_examples(items, stats),
        timelines=timelines,
        timeline_task=timeline_task,
        notes=_notes(record, items, stats, meta, gate, retried_after_halt or {}),
        spend_total_usd=spend_total_usd,
    )


def screenshot_path(run_dir: Path, raw: str) -> Path | None:
    """The file behind a stored evidence path: as written, else under the run's evidence folder
    (a results directory that was moved or copied keeps its screenshots beside the run)."""
    path = Path(raw)
    if path.is_file():
        return path
    folder = run_dir / "evidence"
    if folder.is_dir():
        found = next(folder.rglob(path.name), None)
        if found is not None and found.is_file():
            return found
    return None
