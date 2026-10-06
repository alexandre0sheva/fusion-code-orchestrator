"""``perf``: how fast and how lean is the answer, measured the way a careful engineer would.

For a task with a ``perf`` block the task's hidden workload (``setup(size)`` and ``run(state)``) is
timed in the sandbox at each of several input sizes, in a fresh process each, after a warm-up and at
least five samples. Reported: median and p90 wall time and its noise (median absolute deviation),
peak resident memory, and the **scaling exponent** (the slope of log time against log size, which
exposes an O(n^2) answer that looks fine on a tiny input). The answer is compared with the task's
reference solution, measured the same way: ``ratio_vs_reference`` at the largest size and
``scaling_excess`` (the answer's exponent minus the reference's).

A measurement whose noise is above the task's ``max_noise`` is marked ``unstable`` and carries no
verdict (``ok`` is None): it is measured again next time, never scored. Measurements are taken one
at a time, because a benchmark that shares the machine with another is measuring the other.
"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Literal

from fusion.bench.evaluators.base import Evidence, Memo, read_tree, run_in_sandbox, tree_digest
from fusion.bench.patch import PatchError, apply_patch
from fusion.bench.spec import BenchTask, CodingTruth, PerfSpec, task_hash
from fusion.bench.virtual import offload

__all__ = [
    "PerfEvaluator",
    "SizeSample",
    "Stats",
    "mad",
    "p90",
    "scaling_exponent",
    "summarize",
]

HARNESS_NAME = "__fusion_perf__.py"
HARNESS_SOURCE = Path(__file__).with_name("_perf_harness.py").read_text(encoding="utf-8")
_FLOOR_S = 1e-6  # below the clock's useful resolution
_LOCK = threading.Lock()  # one measurement at a time


@dataclass
class SizeSample:
    """The raw measurements at one input size."""

    size: int
    samples: list[float] = field(default_factory=list)
    peak_rss_mb: float = 0.0
    error: str = ""


@dataclass
class Stats:
    median: float
    p90: float
    mad: float
    n: int

    @property
    def noise(self) -> float:
        """MAD relative to the median: 0.1 means a typical sample is 10% off the median."""
        return self.mad / max(self.median, _FLOOR_S)


Sampler = Callable[[dict[str, str], PerfSpec, int], SizeSample]


def mad(values: list[float]) -> float:
    """Median absolute deviation."""
    centre = median(values)
    return float(median(abs(v - centre) for v in values))


def p90(values: list[float]) -> float:
    """The 90th percentile by nearest rank."""
    ordered = sorted(values)
    return ordered[max(math.ceil(0.9 * len(ordered)) - 1, 0)]


def summarize(samples: list[float]) -> Stats:
    return Stats(median=float(median(samples)), p90=p90(samples), mad=mad(samples), n=len(samples))


def scaling_exponent(sizes: list[int], medians: list[float]) -> float:
    """Least-squares slope of log(time) on log(size): 1 is linear, 2 quadratic."""
    xs = [math.log(s) for s in sizes]
    ys = [math.log(max(m, _FLOOR_S)) for m in medians]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    spread = sum((x - mean_x) ** 2 for x in xs)
    if spread == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / spread


def sandbox_sampler(tree: dict[str, str], spec: PerfSpec, size: int) -> SizeSample:
    """Time the workload at ``size`` in a fresh sandboxed process."""
    cmd = ["python", HARNESS_NAME, spec.script, str(size), str(spec.samples), str(spec.warmup)]
    result, _ = run_in_sandbox(
        tree, cmd, timeout_s=spec.timeout_s, mem_mb=2048, extra={HARNESS_NAME: HARNESS_SOURCE}
    )
    if result.timed_out:
        return SizeSample(size, error=f"timed out after {spec.timeout_s:g}s at size {size}")
    last = (result.stdout.strip().splitlines() or [""])[-1]
    try:
        data = json.loads(last)
        return SizeSample(
            size,
            [float(x) for x in data["samples"]],
            float(data.get("peak_rss_mb", 0.0)),
            str(data.get("error", "")),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        detail = (result.stderr or result.stdout).strip()[-300:]
        return SizeSample(size, error=f"the workload did not report ({detail or 'no output'})")


class PerfEvaluator:
    """Runtime, memory and scaling against the reference solution."""

    name = "perf"

    def __init__(self, sampler: Sampler | None = None) -> None:
        self._sampler = sampler or sandbox_sampler
        self._memo = Memo()
        self._reference: dict[str, list[SizeSample]] = {}

    async def run(self, workdir: Path, task: BenchTask) -> Evidence:
        tree = read_tree(workdir)
        key = f"{task_hash(task)}:{tree_digest(tree)}"
        return await offload(lambda: self._memo.get_or_run(key, lambda: self._measure(tree, task)))

    # -- measuring ------------------------------------------------------------------------------

    def _sample(self, tree: dict[str, str], truth: CodingTruth, spec: PerfSpec) -> list[SizeSample]:
        full = {**tree, **truth.hidden_files}  # the hidden workload goes in last
        with _LOCK:
            return [self._sampler(full, spec, size) for size in spec.sizes]

    def _reference_samples(
        self, task: BenchTask, truth: CodingTruth, spec: PerfSpec
    ) -> list[SizeSample]:
        key = task_hash(task)
        cached = self._reference.get(key)
        if cached is not None:
            return cached
        try:
            changes = apply_patch(task.files, truth.solution)
        except PatchError:
            return []
        tree = {**task.files, **{p: c for p, c in changes.items() if c is not None}}
        for path, content in changes.items():
            if content is None:
                tree.pop(path, None)
        samples = self._sample(tree, truth, spec)
        if all(s.samples and not s.error for s in samples):
            noisy = any(summarize(s.samples).noise > spec.max_noise for s in samples)
            if noisy:  # one more try: a loaded machine usually settles
                samples = self._sample(tree, truth, spec)
            self._reference[key] = samples
        return samples

    def _measure(self, tree: dict[str, str], task: BenchTask) -> Evidence:
        spec = task.artifact_truth().perf
        if spec is None or "expected_pass" not in task.truth:
            return Evidence(
                kind="perf", name=self.name, status="skipped", summary="the task has no perf block"
            )
        truth = CodingTruth.model_validate(task.truth)
        if spec.script not in truth.hidden_files:
            return Evidence(
                kind="perf",
                name=self.name,
                status="skipped",
                summary=f"the workload {spec.script} is not among the task's hidden files",
            )
        answer = self._sample(tree, truth, spec)
        failed = next((s for s in answer if s.error or not s.samples), None)
        if failed is not None:
            reason = f"the workload failed at size {failed.size}: {failed.error or 'no samples'}"
            return Evidence(
                kind="perf", name=self.name, ok=False, metrics={"failed": 1.0}, summary=reason[:400]
            )
        reference = self._reference_samples(task, truth, spec)
        return self._compare(spec, answer, reference)

    def _compare(
        self, spec: PerfSpec, answer: list[SizeSample], reference: list[SizeSample]
    ) -> Evidence:
        stats = [summarize(s.samples) for s in answer]
        medians = [s.median for s in stats]
        top = stats[-1]
        metrics: dict[str, float] = {
            "median_s": top.median,
            "p90_s": top.p90,
            "mad_s": top.mad,
            "noise": max(s.noise for s in stats),
            "peak_rss_mb": max(s.peak_rss_mb for s in answer),
            "scaling_exponent": scaling_exponent(spec.sizes, medians),
            "samples": float(top.n),
            "largest_size": float(spec.sizes[-1]),
        }
        unstable = metrics["noise"] > spec.max_noise
        have_reference = bool(reference) and all(s.samples and not s.error for s in reference)
        parts = [f"median {top.median * 1000:.1f} ms at n={spec.sizes[-1]}"]
        ok: bool | None = None
        if have_reference:
            ref = [summarize(s.samples) for s in reference]
            ref_top = ref[-1]
            ref_exponent = scaling_exponent(spec.sizes, [s.median for s in ref])
            ratio = top.median / max(ref_top.median, _FLOOR_S)
            excess = metrics["scaling_exponent"] - ref_exponent
            metrics.update(
                reference_median_s=ref_top.median,
                reference_scaling_exponent=ref_exponent,
                ratio_vs_reference=ratio,
                scaling_excess=excess,
                rss_ratio_vs_reference=metrics["peak_rss_mb"]
                / max(max(s.peak_rss_mb for s in reference), 1e-3),
            )
            unstable = unstable or max(s.noise for s in ref) > spec.max_noise
            parts.append(f"{ratio:.1f}x the reference, scaling n^{metrics['scaling_exponent']:.2f}")
            ok = ratio <= spec.max_ratio and excess <= spec.scaling_slack
        else:
            parts.append("no reference measurement")
            unstable = True  # nothing to compare with: report, do not score
        status: Literal["measured", "unstable"] = "measured"
        if unstable:
            status, ok = "unstable", None
            parts.append(f"noisy (MAD {metrics['noise']:.0%} of the median): not scored")
        return Evidence(
            kind="perf",
            name=self.name,
            ok=ok,
            metrics=metrics,
            summary="; ".join(parts),
            status=status,
        )
