"""Fusion Code Orchestrator — multi-model coding workflow orchestration."""

import importlib.metadata

try:
    __version__ = importlib.metadata.version("fusion-code-orchestrator")
except importlib.metadata.PackageNotFoundError:  # running from a bare source tree
    __version__ = "0.0.0+unknown"
