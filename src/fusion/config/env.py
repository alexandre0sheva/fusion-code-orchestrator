"""Environment bootstrap for Fusion."""

from __future__ import annotations

import os
from pathlib import Path


def load_env() -> None:
    """Load `.env` from the project root when present."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[3] / ".env",
    ]
    for path in candidates:
        if path.is_file():
            load_dotenv(path, override=False)
            return


def is_test_mode(explicit: bool | None = None) -> bool:
    """Return True when mock routing/providers should be used."""
    if explicit is not None:
        return explicit
    return os.environ.get("FUSION_DEFAULT_PROVIDER", "").strip().lower() == "mock"


def is_local_provider_enabled(provider: str) -> bool:
    """Return True when optional local providers are explicitly enabled."""
    env_key = {
        "ollama": "OLLAMA_ENABLED",
        "lmstudio": "LMSTUDIO_ENABLED",
    }.get(provider)
    if env_key is None:
        return False
    return os.environ.get(env_key, "").strip().lower() in {"1", "true", "yes", "on"}
