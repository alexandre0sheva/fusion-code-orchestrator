"""Pytest configuration — keep tests offline with mock providers and isolated from user state."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _force_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_DEFAULT_PROVIDER", "mock")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_ENABLED", raising=False)
    monkeypatch.delenv("LMSTUDIO_ENABLED", raising=False)


@pytest.fixture(autouse=True)
def fusion_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point user config, user data and project config at an empty temp tree.

    Without this a developer's real ``~/.config/fusion`` or ``./.fusion`` would leak into tests.
    """
    home = tmp_path / "fusion-home"
    for name in ("config", "data", "project"):
        (home / name).mkdir(parents=True)
    monkeypatch.setenv("FUSION_CONFIG_DIR", str(home / "config"))
    monkeypatch.setenv("FUSION_DATA_DIR", str(home / "data"))
    monkeypatch.setenv("FUSION_PROJECT_DIR", str(home / "project"))
    monkeypatch.delenv("FUSION_DB_PATH", raising=False)
    for key in [k for k in os.environ if k.upper().startswith("FUSION__")]:
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture
def three_model_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``budget: medium`` run the three-model ``panel-cheap``, as it did before 0.2.0 chose
    ``panel-duo``. For tests of pipeline mechanics (quorum, claims, golden outputs) that need three
    panelists; tests of the shipped default itself do not use it."""
    monkeypatch.setenv("FUSION__BUDGET_STRATEGIES__MEDIUM", "panel-cheap")
