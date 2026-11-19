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
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("FUSION_OPENROUTER_PRIVACY", raising=False)
    # The repo's .env is loaded at CLI import; a project-root override from it would make the
    # workspace tests read this repository instead of their temp project.
    monkeypatch.delenv("FUSION_READ_ROOT", raising=False)
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
def wide_consoles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the CLI's module-level Rich consoles a fixed 200-column width.

    Rich shortens table headers in a narrow terminal. It reads ``COLUMNS`` only when it does not
    take the output for a dumb terminal, and CI can look like one (``TERM=dumb`` plus a forced
    terminal), so tests that read table text pin the width instead of passing ``COLUMNS``.
    """
    from fusion.bench import cli as bench_cli
    from fusion.cli import common, models_cmd

    for console in (bench_cli.console, common.console, models_cmd.console):
        # Rich uses a fixed size only when both are set; with one missing it detects the terminal.
        monkeypatch.setattr(console, "_width", 200)
        monkeypatch.setattr(console, "_height", 50)


@pytest.fixture
def three_model_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``budget: medium`` run the three-model ``panel-nano``, as it did before 0.2.0 chose
    ``panel-duo``. For tests of pipeline mechanics (quorum, claims, golden outputs) that need three
    panelists; tests of the shipped default itself do not use it."""
    monkeypatch.setenv("FUSION__BUDGET_STRATEGIES__MEDIUM", "panel-nano")
