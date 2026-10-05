"""`fusion init`, `fusion config show/paths` and the global --set flag."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from fusion.cli.app import app
from fusion.config.layers import resolve_config, set_cli_overrides
from fusion.storage.run_store import RunStore

runner = CliRunner()


def _legacy(path: Path) -> None:
    store = RunStore(db_path=str(path))
    store.create_run(task_type="ask", input_data={}, sanitized_input={})
    store.close()


def test_init_writes_a_commented_starter_config_that_changes_nothing(fusion_home: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    target = fusion_home / "config" / "config.yaml"
    text = target.read_text()
    assert text.lstrip().startswith("#")
    assert "provider_limits" in text and "ANTHROPIC_API_KEY" in result.output
    assert yaml.safe_load(text) is None  # every setting is commented out
    assert resolve_config().data["fanout"]["max_concurrency"] == 6
    assert "fusion config show --resolved" in result.output


def test_init_does_not_overwrite_without_force(fusion_home: Path) -> None:
    target = fusion_home / "config" / "config.yaml"
    target.write_text("fanout:\n  max_concurrency: 9\n")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert "already exists" in result.output
    assert "max_concurrency: 9" in target.read_text()
    forced = runner.invoke(app, ["init", "--force"])
    assert forced.exit_code == 0
    assert "max_concurrency: 9" not in target.read_text()


def test_init_import_legacy_copies_the_cwd_database(
    fusion_home: Path, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    work = tmp_path / "work"
    work.mkdir()
    _legacy(work / "fusion_runs.db")
    monkeypatch.chdir(work)
    result = runner.invoke(app, ["init", "--import-legacy"])
    assert result.exit_code == 0, result.output
    assert "Imported 1 run" in result.output
    assert (work / "fusion_runs.db").exists()
    store = RunStore(db_path=str(fusion_home / "data" / "runs.db"))
    assert store.get_stats().total_runs == 1
    store.close()


def test_init_import_legacy_without_a_legacy_db_is_a_clear_error(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "--import-legacy"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_a_plain_init_never_imports_the_legacy_db(
    fusion_home: Path, tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _legacy(tmp_path / "fusion_runs.db")
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert not (fusion_home / "data" / "runs.db").exists()


def test_config_paths_lists_every_location(fusion_home: Path) -> None:
    result = runner.invoke(app, ["config", "paths"])
    assert result.exit_code == 0
    assert str(fusion_home / "config" / "config.yaml") in result.output
    assert str(fusion_home / "project" / ".fusion" / "config.yaml") in result.output
    assert str(fusion_home / "data" / "runs.db") in result.output


def test_config_show_resolved_prints_values_with_their_origin(fusion_home: Path) -> None:
    user = fusion_home / "config" / "config.yaml"
    user.write_text("fanout:\n  max_concurrency: 4\n")
    result = runner.invoke(app, ["config", "show", "--resolved", "--json"])
    assert result.exit_code == 0, result.output
    rows = {r["key"]: r for r in json.loads(result.output)}
    assert rows["fanout.max_concurrency"] == {
        "key": "fanout.max_concurrency",
        "value": 4,
        "origin": f"user:{user}",
    }
    assert rows["fanout.global_timeout_seconds"]["origin"] == "packaged"


def test_config_show_resolved_table_can_be_filtered(fusion_home: Path) -> None:
    result = runner.invoke(app, ["config", "show", "--resolved", "--filter", "fanout"])
    assert result.exit_code == 0
    assert "max_concurrency" in result.output
    assert "gpt-luna" not in result.output


def test_config_show_without_resolved_lists_layers(fusion_home: Path) -> None:
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "packaged" in result.output and "user" in result.output


def test_global_set_flag_overrides_everything(fusion_home: Path) -> None:
    try:
        result = runner.invoke(
            app,
            ["--set", "fanout.max_concurrency=1", "config", "show", "--resolved", "--json"],
        )
        assert result.exit_code == 0, result.output
        rows = {r["key"]: r for r in json.loads(result.output)}
        assert rows["fanout.max_concurrency"]["value"] == 1
        assert rows["fanout.max_concurrency"]["origin"] == "cli"
    finally:
        set_cli_overrides({})


def test_invalid_config_is_reported_without_a_traceback(fusion_home: Path) -> None:
    (fusion_home / "config" / "config.yaml").write_text("fanout:\n  max_concurrency: 0\n")
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1
    assert "fanout.max_concurrency" in result.output
    assert "Traceback" not in result.output
