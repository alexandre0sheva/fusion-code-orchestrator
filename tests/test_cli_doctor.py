"""`fusion doctor`: every check, its fix text, the exit code and the JSON shape."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from typer.testing import CliRunner

from fusion.cli import doctor as doctor_module
from fusion.cli.doctor import Check, default_mcp_check, mask, run_checks
from fusion.cli.main import app
from fusion.config.catalog_check import ModelCheck
from fusion.install.common import InstallError, ServerSpec, verify_server

SECRET = "sk-ant-api03-SECRETVALUE0123456789-abcd"
ALL_KEYS = {
    "ANTHROPIC_API_KEY": SECRET,
    "OPENAI_API_KEY": "sk-proj-OTHERSECRET9876543210-wxyz",
    "GOOGLE_API_KEY": "AIzaGOOGLESECRET0123456789-qrst",
}


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty home and project, so client configs on the real machine are never read."""
    home = tmp_path / "doctor-home"
    project = tmp_path / "doctor-project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(project)
    return project


def named(checks: list[Check], name: str) -> list[Check]:
    return [c for c in checks if c.name == name]


def run(machine: Path, env: dict[str, str] | None = None, **kwargs: Any) -> list[Check]:
    return run_checks(
        environ=env or {},
        project_dir=machine,
        home=machine.parent / "doctor-home",
        mcp=kwargs.pop("mcp", False),
        **kwargs,
    )


def test_a_key_is_shown_masked_never_whole(machine: Path) -> None:
    checks = run(machine, ALL_KEYS)
    for provider in ("anthropic", "openai", "google"):
        (key,) = named(checks, f"key:{provider}")
        assert key.status == "ok"
    text = " ".join(c.detail for c in checks)
    assert "abcd" in text and "SECRETVALUE" not in text and SECRET not in text
    assert mask("short") == "set" and mask("a-long-enough-key-9f2c") == "…9f2c"


def test_with_no_key_at_all_every_provider_is_an_error_with_the_fix(machine: Path) -> None:
    keys = [c for c in run(machine) if c.name.startswith("key:")]
    assert keys and all(c.status == "error" for c in keys)
    assert all("export " in c.fix and ".env" in c.fix for c in keys)


def test_one_missing_key_is_only_a_warning(machine: Path) -> None:
    env = {k: v for k, v in ALL_KEYS.items() if k != "GOOGLE_API_KEY"}
    (google,) = named(run(machine, env), "key:google")
    assert google.status == "warn" and "skipped" in google.detail


def test_runtime_checks_name_python_and_uv(machine: Path) -> None:
    checks = run(machine, ALL_KEYS)
    assert named(checks, "python")[0].status == "ok"
    assert str(sys.version_info[0]) in named(checks, "python")[0].detail
    assert named(checks, "fusion")[0].detail.startswith("fusion-code-orchestrator")
    assert named(checks, "uv")[0].status in {"ok", "warn"}


def test_a_broken_config_is_an_error_naming_the_file(machine: Path, fusion_home: Path) -> None:
    (fusion_home / "config" / "config.yaml").write_text("fanout: [unclosed")
    (config,) = named(run(machine, ALL_KEYS), "config")
    assert config.status == "error" and "config.yaml" in config.detail
    checks = run(machine, ALL_KEYS, live=True)  # the rest still reports, and nothing crashes
    assert named(checks, "keys, catalog")[0].status == "skip"
    assert not [c for c in checks if c.name.startswith(("key:", "live"))]
    assert named(checks, "database") and named(checks, "client:cursor")


def test_a_check_that_crashes_is_reported_not_raised(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> Any:
        raise OSError("disk gone")

    monkeypatch.setattr(doctor_module, "_database", boom)
    (database,) = named(run(machine, ALL_KEYS), "database")
    assert database.status == "error" and "disk gone" in database.detail


def test_the_catalog_check_reports_stale_prices_with_a_fix(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_module, "catalog_warnings", lambda _c: ["claude-x: price is old"])
    (catalog,) = named(run(machine, ALL_KEYS), "catalog")
    assert catalog.status == "warn" and "price is old" in catalog.detail
    assert "fusion models check --live" in catalog.fix


def test_an_unwritable_database_folder_is_an_error(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can write anywhere")
    locked = machine / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        monkeypatch.setenv("FUSION_DB_PATH", str(locked / "sub" / "runs.db"))
        (db,) = named(run(machine, ALL_KEYS), "database")
    finally:
        locked.chmod(0o700)
    assert db.status == "error" and "not writable" in db.detail and "FUSION_DB_PATH" in db.fix


def test_a_writable_database_is_ok_before_it_exists(
    machine: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION_DB_PATH", str(machine / "new" / "deeper" / "runs.db"))
    (db,) = named(run(machine, ALL_KEYS), "database")
    assert db.status == "ok" and "will be created" in db.detail
    assert not (machine / "new").exists()  # a check never creates anything


def test_the_mcp_check_passes_or_reports_the_server_failure(machine: Path) -> None:
    ok = run(machine, ALL_KEYS, mcp=True, mcp_check=lambda: ["fusion_ask", "fusion_stats"])
    (good,) = named(ok, "mcp-server")
    assert good.status == "ok" and "2 tools" in good.detail

    def broken() -> list[str]:
        raise InstallError("Could not start the server: Invalid JSON: expected value\nlog line 2")

    (bad,) = named(run(machine, ALL_KEYS, mcp=True, mcp_check=broken), "mcp-server")
    assert (
        bad.status == "error"
        and bad.detail == "Could not start the server: Invalid JSON: expected value"
    )
    assert "stdout" in bad.fix


def test_the_real_server_starts_clean_on_the_mock_provider() -> None:
    names = default_mcp_check()
    assert "fusion_ask" in names and "fusion_stats" in names


def test_a_server_that_prints_to_stdout_fails_the_check() -> None:
    noisy = ServerSpec(sys.executable, ("-c", "print('hello'); import time; time.sleep(30)"))
    with pytest.raises(InstallError):
        verify_server(noisy, timeout=20)


def test_client_setups_are_listed_and_unused_clients_are_info_not_faults(machine: Path) -> None:
    checks = run(machine, ALL_KEYS)
    clients = {c.name: c for c in checks if c.name.startswith("client:")}
    assert set(clients) == {"client:claude-code", "client:cursor", "client:codex"}
    assert all(c.status == "info" and c.detail == "not configured" for c in clients.values())
    assert clients["client:cursor"].fix == "fusion install cursor"

    (machine / ".cursor").mkdir()
    (machine / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"fusion": {"command": "uvx", "args": ["fusion", "mcp"]}}})
    )
    (cursor,) = named(run(machine, ALL_KEYS), "client:cursor")
    assert cursor.status == "ok"

    (machine / ".cursor" / "mcp.json").write_text("{")
    (cursor,) = named(run(machine, ALL_KEYS), "client:cursor")
    assert cursor.status == "error" and "not valid JSON" in cursor.detail


def test_live_asks_providers_only_when_asked_and_only_for_keys_that_exist(machine: Path) -> None:
    calls: list[dict[str, str]] = []

    def fake(keys: Any) -> list[ModelCheck]:
        calls.append(dict(keys))
        return [
            ModelCheck(alias="a", provider="anthropic", model_id="a-1", status="ok"),
            ModelCheck(
                alias="b",
                provider="openai",
                model_id="b-1",
                status="unknown_id",
                detail="not listed",
            ),
            ModelCheck(
                alias="c",
                provider="google",
                model_id="c-1",
                status="skipped",
                detail="no API key for google",
            ),
        ]

    assert not [c for c in run(machine, ALL_KEYS) if c.name.startswith("live")]
    checks = run(machine, {"ANTHROPIC_API_KEY": SECRET}, live=True, live_check=fake)
    assert calls == [{"anthropic": SECRET}]
    by_name = {c.name: c for c in checks if c.name.startswith("live:")}
    assert by_name["live:a"].status == "ok"
    assert by_name["live:b"].status == "error" and "not listed" in by_name["live:b"].detail
    assert by_name["live:c"].status == "skip"
    (skipped,) = [c for c in run(machine, live=True, live_check=fake) if c.name == "live"]
    assert skipped.status == "skip" and len(calls) == 1  # no key, so nothing was asked


# -- the command ----------------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ALL_KEYS.items():
        monkeypatch.setenv(name, value)


def test_doctor_exits_0_when_nothing_is_wrong_and_never_prints_a_secret(
    runner: CliRunner, machine: Path, keys: None
) -> None:
    result = runner.invoke(app, ["doctor", "--no-mcp"])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY is set (…abcd)" in result.stdout
    assert "SECRETVALUE" not in result.output and "fix:" not in result.stdout.split("catalog")[0]
    assert result.stdout.rstrip().endswith(("Everything checks out.", "warning(s)."))


def test_doctor_exits_1_and_says_how_to_fix_a_missing_key(runner: CliRunner, machine: Path) -> None:
    result = runner.invoke(app, ["doctor", "--no-mcp"])
    assert result.exit_code == 1
    assert (
        "FAIL  key:anthropic" in result.stdout and "fix: export ANTHROPIC_API_KEY" in result.stdout
    )
    assert "problem(s)" in result.stdout


def test_doctor_strict_makes_warnings_fail(runner: CliRunner, machine: Path, keys: None) -> None:
    (machine / ".cursor").mkdir()
    (machine / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"fusion": {"command": "node", "args": ["x.js"]}}})
    )
    assert runner.invoke(app, ["doctor", "--no-mcp"]).exit_code == 0
    strict = runner.invoke(app, ["doctor", "--no-mcp", "--strict"])
    assert strict.exit_code == 1 and "WARN  client:cursor" in strict.stdout


def test_doctor_json_has_a_stable_shape(runner: CliRunner, machine: Path, keys: None) -> None:
    result = runner.invoke(app, ["doctor", "--no-mcp", "--json"])
    assert result.exit_code == 0, result.output
    schema = {
        "type": "object",
        "required": ["ok", "checks"],
        "properties": {
            "ok": {"type": "boolean"},
            "checks": {
                "type": "array",
                "minItems": 5,
                "items": {
                    "type": "object",
                    "required": ["name", "status", "detail", "fix"],
                    "properties": {"status": {"enum": ["ok", "warn", "error", "info", "skip"]}},
                },
            },
        },
    }
    jsonschema.validate(json.loads(result.stdout), schema)
    failed = json.loads(
        runner.invoke(app, ["doctor", "--no-mcp", "--json"], env={"ANTHROPIC_API_KEY": ""}).stdout
    )
    assert isinstance(failed["ok"], bool)


def test_doctor_starts_the_mcp_server_by_default(
    runner: CliRunner, machine: Path, keys: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_module, "default_mcp_check", lambda: ["fusion_ask"] * 8)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0 and "mcp-server" in result.stdout and "8 tools" in result.stdout
    skipped = runner.invoke(app, ["doctor", "--no-mcp"])
    assert "mcp-server" not in skipped.stdout


def test_doctor_output_is_stable_without_a_terminal(
    runner: CliRunner, machine: Path, keys: None
) -> None:
    lines = runner.invoke(app, ["doctor", "--no-mcp"]).stdout.splitlines()
    assert all("\x1b" not in line for line in lines)
    assert any(line.startswith("ok    python") for line in lines)
    assert any(line.startswith("info  client:codex") for line in lines)
