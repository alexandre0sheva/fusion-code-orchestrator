"""Evaluators: measure what an answer built. See ``base`` for the interface.

``EvaluatorSet`` is the named evaluators one study uses. The ones with a cache (hidden tests, perf,
the browser capture) live as long as the set, so build one per study, not per answer.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path

from fusion.bench.evaluators.a11y import A11yEvaluator
from fusion.bench.evaluators.base import Evaluator, Evidence, ToolSpec
from fusion.bench.evaluators.perf import PerfEvaluator, Sampler
from fusion.bench.evaluators.static import DiffStatsEvaluator, StaticEvaluator
from fusion.bench.evaluators.tests import BuildEvaluator, TestsEvaluator
from fusion.bench.evaluators.visual import (
    BrowserDriver,
    CaptureCache,
    ConsoleEvaluator,
    PlaywrightDriver,
    VisualEvaluator,
)
from fusion.bench.spec import BenchTask, Category, EvidenceKind

__all__ = [
    "DEFAULT_EVALUATORS",
    "Evaluator",
    "EvaluatorSet",
    "Evidence",
    "ToolSpec",
]

# The evaluators run for a category when the task does not name its own, in this order. Perf runs
# last of the CPU-heavy ones, and alone, so that nothing else is competing for the processor.
DEFAULT_EVALUATORS: dict[Category, tuple[str, ...]] = {
    "coding": ("tests", "build", "static", "diff_stats"),
    "frontend": ("tests", "build", "static", "diff_stats", "a11y", "visual", "console"),
    "performance": ("tests", "build", "static", "diff_stats", "perf"),
    "code_review": (),
    "debugging": (),
    "architecture": (),
    "planning": (),
}
_KIND_OF: Mapping[str, EvidenceKind] = {
    "tests": "tests",
    "build": "build",
    "static": "static",
    "diff_stats": "diff_stats",
    "perf": "perf",
    "visual": "screenshot",
    "console": "console",
    "a11y": "a11y",
}


class EvaluatorSet:
    """The evaluators of a study, by name. ``artifacts_dir`` is where screenshots are kept;
    ``driver`` opens pages (the Playwright driver by default); ``sampler`` times a workload."""

    def __init__(
        self,
        artifacts_dir: Path | None = None,
        *,
        driver: BrowserDriver | None = None,
        sampler: Sampler | None = None,
    ) -> None:
        cache = CaptureCache(driver or PlaywrightDriver(), artifacts_dir)
        self.cache = cache
        self._by_name: dict[str, Evaluator] = {
            e.name: e
            for e in (
                TestsEvaluator(),
                BuildEvaluator(),
                StaticEvaluator(),
                DiffStatsEvaluator(),
                PerfEvaluator(sampler),
                VisualEvaluator(cache),
                ConsoleEvaluator(cache),
                A11yEvaluator(cache),
            )
        }

    @property
    def names(self) -> list[str]:
        return list(self._by_name)

    def names_for(self, task: BenchTask) -> list[str]:
        """The evaluators for ``task``: those it names, else its category's defaults."""
        named = task.truth.get("evaluators")
        chosen = [str(n) for n in named] if named else list(DEFAULT_EVALUATORS[task.category])
        return [n for n in chosen if n in self._by_name]

    def get(self, name: str) -> Evaluator | None:
        return self._by_name.get(name)

    async def run_one(self, name: str, workdir: Path, task: BenchTask) -> Evidence:
        """Run one evaluator, never raising: a crash is evidence that could not be taken."""
        began = time.monotonic()
        evaluator = self._by_name[name]
        try:
            evidence = await evaluator.run(workdir, task)
        except Exception as exc:  # noqa: BLE001 — an evaluator failing is a finding, not a crash
            evidence = Evidence(
                kind=_KIND_OF[name],
                name=name,
                status="skipped",
                summary=f"the evaluator failed: {type(exc).__name__}: {str(exc)[:200]}",
            )
        evidence.name = evidence.name or name
        evidence.seconds = time.monotonic() - began
        return evidence

    async def collect(
        self, workdir: Path, task: BenchTask, names: list[str] | None = None
    ) -> list[Evidence]:
        """Evidence from each evaluator in turn, numbered E1, E2... for judges to cite."""
        found: list[Evidence] = []
        for name in names if names is not None else self.names_for(task):
            evidence = await self.run_one(name, workdir, task)
            evidence.id = f"E{len(found) + 1}"
            found.append(evidence)
        return found
