"""Implementation benchmarks and shadow baseline comparison."""

from typing import Any

__all__ = ["compare_implementations"]


def __getattr__(name: str) -> Any:
    # Lazy import: compare pulls in orchestration pipelines, which themselves
    # import fusion.benchmark.shadow — eager import here would be circular.
    if name == "compare_implementations":
        from fusion.benchmark.compare import compare_implementations

        return compare_implementations
    raise AttributeError(name)
