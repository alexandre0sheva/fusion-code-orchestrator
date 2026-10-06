"""`fusion install cursor`: merges, stays idempotent, supports --dry-run, refuses bad input."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from fusion.cli.app import app
from fusion.install.common import InstallError, ServerSpec, integration_text, uvx_spec
from fusion.install.cursor import CursorOptions, inspect_cursor, install_cursor

SPEC = uvx_spec()


def run(project: Path, home: Path, **options: Any) -> Any:
    seen: list[ServerSpec] = []

    def verifier(spec: ServerSpec) -> list[str]:
        seen.append(spec)
        return ["fusion_ask", "fusion_stats"]

    report = install_cursor(
        CursorOptions(project_dir=project, **options), verifier=verifier, home=home
    )
    report.verified = seen
    return report


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def mcp(project: Path) -> dict[str, Any]:
    return json.loads((project / ".cursor" / "mcp.json").read_text())


def test_the_shipped_mcp_json_is_exactly_what_the_installer_writes(
    project: Path, home: Path
) -> None:
    run(project, home)
    assert mcp(project) == json.loads(integration_text("cursor", "mcp.json"))
    assert mcp(project)["mcpServers"]["fusion"] == SPEC.as_entry()


def test_a_project_install_writes_the_server_and_the_rule(project: Path, home: Path) -> None:
    report = run(project, home)
    rule = project / ".cursor" / "rules" / "fusion.mdc"
    assert rule.read_text() == integration_text("cursor", "rules", "fusion.mdc")
    assert report.changed and report.verified == [SPEC]
    assert not (home / ".cursor").exists()


def test_the_rule_is_an_intelligently_applied_cursor_rule() -> None:
    match = re.match(r"---\n(.*?)\n---\n", integration_text("cursor", "rules", "fusion.mdc"), re.S)
    assert match
    meta = yaml.safe_load(match.group(1))
    # alwaysApply false + a description + no globs = "Apply Intelligently": the agent decides.
    assert meta["alwaysApply"] is False and "globs" not in meta
    assert "Do not use for trivial edits" in meta["description"]


def test_other_servers_and_keys_survive_the_merge(project: Path, home: Path) -> None:
    (project / ".cursor").mkdir()
    existing = {
        "mcpServers": {"github": {"url": "https://example.test/mcp", "headers": {"A": "b"}}},
        "somethingElse": [1, 2],
    }
    (project / ".cursor" / "mcp.json").write_text(json.dumps(existing))
    run(project, home)
    merged = mcp(project)
    assert merged["mcpServers"]["github"] == existing["mcpServers"]["github"]
    assert merged["somethingElse"] == [1, 2] and "fusion" in merged["mcpServers"]


def test_running_it_twice_changes_nothing_the_second_time(project: Path, home: Path) -> None:
    run(project, home)
    before = {p: p.read_text() for p in project.rglob("*") if p.is_file()}
    report = run(project, home)
    assert not report.changed and len(report.unchanged) == 2
    assert {p: p.read_text() for p in project.rglob("*") if p.is_file()} == before


def test_a_different_fusion_server_is_refused_without_force_and_nothing_is_written(
    project: Path, home: Path
) -> None:
    (project / ".cursor").mkdir()
    text = json.dumps({"mcpServers": {"fusion": {"command": "uv", "args": ["run", "fusion"]}}})
    (project / ".cursor" / "mcp.json").write_text(text)
    with pytest.raises(InstallError, match="--force"):
        run(project, home)
    assert (project / ".cursor" / "mcp.json").read_text() == text
    assert not (project / ".cursor" / "rules").exists()
    run(project, home, force=True)
    assert mcp(project)["mcpServers"]["fusion"]["command"] == "uvx"


def test_a_malformed_config_is_refused_untouched(project: Path, home: Path) -> None:
    (project / ".cursor").mkdir()
    (project / ".cursor" / "mcp.json").write_text('{"mcpServers": ')
    with pytest.raises(InstallError, match="not valid JSON"):
        run(project, home)
    assert (project / ".cursor" / "mcp.json").read_text() == '{"mcpServers": '
    assert not (project / ".cursor" / "rules").exists()


def test_a_dry_run_writes_nothing_and_says_what_it_would_do(project: Path, home: Path) -> None:
    report = run(project, home, dry_run=True)
    assert not list(project.rglob("*")) and not list(home.rglob("*"))
    assert report.verified == [] and report.commands  # the launch check is described, not run
    assert all(line.startswith("Would") for line in report.actions) and len(report.actions) == 2


def test_global_writes_the_home_config_and_no_rule(project: Path, home: Path) -> None:
    report = run(project, home, global_=True)
    assert json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]["fusion"]
    assert not (project / ".cursor").exists()
    assert any("no global rules file" in note for note in report.notes)


def test_an_edited_rule_is_kept_unless_forced_and_no_rules_skips_it(
    project: Path, home: Path
) -> None:
    run(project, home)
    rule = project / ".cursor" / "rules" / "fusion.mdc"
    rule.write_text("my own rule\n")
    with pytest.raises(InstallError, match="--no-rules"):
        run(project, home)
    assert rule.read_text() == "my own rule\n"
    assert not run(project, home, rules=False).changed
    run(project, home, force=True)
    assert rule.read_text() == integration_text("cursor", "rules", "fusion.mdc")


def test_a_server_that_does_not_start_is_not_registered(project: Path, home: Path) -> None:
    def broken(_spec: ServerSpec) -> list[str]:
        raise InstallError("no start")

    with pytest.raises(InstallError, match="no start"):
        install_cursor(CursorOptions(project_dir=project), verifier=broken, home=home)
    assert not list(project.rglob("*"))


def test_ref_and_local_checkout_choose_the_command(project: Path, home: Path) -> None:
    run(project, home, ref="v0.2.0")
    assert mcp(project)["mcpServers"]["fusion"]["args"][-3].endswith("@v0.2.0")
    root = Path(__file__).resolve().parents[1]
    run(project, home, checkout=root, force=True)
    assert mcp(project)["mcpServers"]["fusion"]["command"] == "uv"


# -- doctor hook ---------------------------------------------------------------------------------


def test_inspect_reports_absent_ok_warn_and_error(project: Path, home: Path) -> None:
    project_finding, user_finding = inspect_cursor(project_dir=project, home=home)
    assert project_finding.status == user_finding.status == "absent"
    assert user_finding.fix == "fusion install cursor --global"

    run(project, home)
    assert inspect_cursor(project_dir=project, home=home)[0].status == "ok"

    path = project / ".cursor" / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"fusion": {"command": "node", "args": ["x.js"]}}}))
    assert inspect_cursor(project_dir=project, home=home)[0].status == "warn"

    path.write_text("{")
    broken = inspect_cursor(project_dir=project, home=home)[0]
    assert broken.status == "error" and "not valid JSON" in broken.message


# -- the command line ----------------------------------------------------------------------------


def test_cli_dry_run_prints_the_plan_and_writes_nothing(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    result = CliRunner().invoke(app, ["install", "cursor", "--dry-run", "--no-verify"])
    assert result.exit_code == 0, result.output
    assert "Would write the 'fusion' server" in result.output and not list(project.rglob("*"))


def test_cli_installs_and_refuses_with_exit_code_one(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    ok = CliRunner().invoke(app, ["install", "cursor", "--no-verify"])
    assert ok.exit_code == 0 and (project / ".cursor" / "mcp.json").exists()
    (project / ".cursor" / "mcp.json").write_text("{")
    bad = CliRunner().invoke(app, ["install", "cursor", "--no-verify"])
    assert bad.exit_code == 1 and "not valid JSON" in bad.output
