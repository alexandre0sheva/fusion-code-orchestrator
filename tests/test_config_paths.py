"""User directories and database location: nothing is created in the host's cwd."""

from __future__ import annotations

from pathlib import Path

import pytest

from fusion.config import paths


def test_default_db_lives_in_the_user_data_dir(fusion_home: Path) -> None:
    assert paths.user_data_dir() == fusion_home / "data"
    assert paths.default_db_path() == fusion_home / "data" / "runs.db"


def test_fusion_db_path_env_overrides_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere" / "my.db"
    monkeypatch.setenv("FUSION_DB_PATH", str(target))
    assert paths.resolve_db_path() == target


def test_explicit_db_path_wins_over_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FUSION_DB_PATH", str(tmp_path / "env.db"))
    assert paths.resolve_db_path(str(tmp_path / "arg.db")) == tmp_path / "arg.db"


def test_tilde_is_expanded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.resolve_db_path("~/x.db") == tmp_path / "x.db"


def test_without_overrides_dirs_come_from_platformdirs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FUSION_CONFIG_DIR", "FUSION_DATA_DIR"):
        monkeypatch.delenv(name)
    assert paths.user_config_dir().name == "fusion"
    assert paths.user_data_dir().name == "fusion"


def test_project_config_is_read_from_the_project_dir(fusion_home: Path) -> None:
    assert paths.project_config_file() == fusion_home / "project" / ".fusion" / "config.yaml"


def test_project_dir_defaults_to_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("FUSION_PROJECT_DIR")
    monkeypatch.chdir(tmp_path)
    assert paths.project_dir() == tmp_path


def test_legacy_db_is_the_cwd_relative_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert paths.legacy_db_path() == tmp_path / "fusion_runs.db"
