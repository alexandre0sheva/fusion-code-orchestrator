"""Version has a single source (pyproject.toml) and the plugin manifest must not drift."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

import fusion
from fusion.cli.app import app

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def test_package_version_comes_from_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "9.9.9")
    reloaded = importlib.reload(fusion)
    try:
        assert reloaded.__version__ == "9.9.9"
    finally:
        monkeypatch.undo()
        importlib.reload(fusion)


def test_package_version_falls_back_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", _missing)
    reloaded = importlib.reload(fusion)
    try:
        assert reloaded.__version__ == "0.0.0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(fusion)


def test_installed_version_matches_pyproject() -> None:
    assert fusion.__version__ == _pyproject_version()


def test_plugin_manifest_version_matches_pyproject() -> None:
    path = ROOT / "plugin" / ".claude-plugin" / "plugin.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["version"] == _pyproject_version(), (
        "plugin/.claude-plugin/plugin.json version drifted from pyproject.toml; bump both together"
    )


def test_cli_version_prints_package_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"v{fusion.__version__}" in result.output


def test_lock_file_records_the_project_version() -> None:
    """CI installs with ``--locked``, which fails when ``uv.lock`` still names the old version."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    entry = next(p for p in lock["package"] if p["name"] == "fusion-code-orchestrator")
    assert entry["version"] == _pyproject_version(), "run `uv lock` after bumping the version"
