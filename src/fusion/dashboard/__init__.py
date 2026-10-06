"""``fusion dashboard``: a read-only local web view of runs, spend, benchmarks and configuration."""

from fusion.dashboard.app import create_app, serve

__all__ = ["create_app", "serve"]
