"""The program that times a performance task's workload. Stdlib only: its source is copied into the
sandbox and run there as ``python __fusion_perf__.py SCRIPT SIZE SAMPLES WARMUP``.

``SCRIPT`` (a file of the task's hidden tests) defines ``setup(size)``, which builds the input, and
``run(state)``, the workload being timed. Each sample builds a fresh input, collects garbage, then
times one ``run``. A fast workload (a fraction of a millisecond) would finish its few samples inside
one scheduler hiccup of a shared machine, which then inflates every sample alike and passes for a
slow answer, so sampling goes on past ``SAMPLES`` until the series spans ``MIN_SPAN_S`` of wall time
(at most ``MAX_SAMPLES``). The program prints one JSON object: ``samples`` (seconds each),
``peak_rss_mb`` (the process's peak resident memory) and ``error`` (empty unless the workload
raised).
``measure`` takes the clock as an argument so its arithmetic can be tested without real time.
"""

from __future__ import annotations

import gc
import importlib.util
import json
import os
import sys
import time
import traceback
from collections.abc import Callable
from typing import Any

MIN_SPAN_S = 0.1  # a series shorter than this keeps sampling: one stall must not cover all of it
MAX_SAMPLES = 99


def measure(
    setup: Callable[[int], Any],
    run: Callable[[Any], object],
    size: int,
    samples: int,
    warmup: int,
    *,
    clock: Callable[[], float] = time.perf_counter,
    collect: Callable[[], object] = gc.collect,
    min_span_s: float = MIN_SPAN_S,
    max_samples: int = MAX_SAMPLES,
) -> list[float]:
    """Seconds taken by timed runs, after ``warmup`` runs that are not kept.

    At least ``samples`` runs are timed, and more (up to ``max_samples``) until the timed series,
    setup and collection between runs included, has lasted ``min_span_s``.
    """
    taken: list[float] = []
    for _ in range(warmup):
        run(setup(size))
    series_began = clock()
    while len(taken) < max(samples, 1):
        taken.append(_time_one(setup, run, size, clock, collect))
    while len(taken) < max_samples and clock() - series_began < min_span_s:
        taken.append(_time_one(setup, run, size, clock, collect))
    return taken


def _time_one(
    setup: Callable[[int], Any],
    run: Callable[[Any], object],
    size: int,
    clock: Callable[[], float],
    collect: Callable[[], object],
) -> float:
    state = setup(size)
    collect()
    began = clock()
    run(state)
    return clock() - began


def peak_rss_mb() -> float:
    try:
        import resource
    except ImportError:  # not on this platform
        return 0.0
    peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def load(script: str) -> tuple[Callable[[int], Any], Callable[[Any], object]]:
    spec = importlib.util.spec_from_file_location("fusion_bench_workload", script)
    if spec is None or spec.loader is None:
        msg = f"cannot load {script}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.setup, module.run


def main(argv: list[str]) -> int:
    script, size, samples, warmup = argv[0], int(argv[1]), int(argv[2]), int(argv[3])
    sys.path.insert(0, os.getcwd())
    result: dict[str, Any] = {"samples": [], "peak_rss_mb": 0.0, "error": ""}
    try:
        setup, run = load(script)
        result["samples"] = measure(setup, run, size, samples, warmup)
    except Exception:  # noqa: BLE001 — the workload is the answer's code; report, do not crash
        result["error"] = traceback.format_exc(limit=3)[-600:]
    result["peak_rss_mb"] = peak_rss_mb()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
