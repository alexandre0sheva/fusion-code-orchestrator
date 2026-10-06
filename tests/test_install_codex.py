"""`fusion install codex`: safe TOML edits that keep comments and other servers, idempotent."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fusion.cli.app import app
from fusion.install.checks import inspect_clients
from fusion.install.codex import (
    BEGIN,
    END,
    PROVIDER_KEYS,
    STARTUP_TIMEOUT_S,
    TOOL_TIMEOUT_S,
    CodexOptions,
    inspect_codex,
    install_codex,
    with_agents_block,
)
from fusion.install.common import InstallError, ServerSpec, integration_text, uvx_spec

SPEC = uvx_spec()
USER_CONFIG = """\
# my codex settings
model = "o4-mini"   # keep this comment

[mcp_servers.github]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-github"]
tool_timeout_sec = 30  # slow network
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    (path / ".codex").mkdir(parents=True)
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    return path


def run(home: Path, project_dir: Path, /, **options: Any) -> Any:
    seen: list[ServerSpec] = []

    def verifier(spec: ServerSpec) -> list[str]:
        seen.append(spec)
        return ["fusion_ask"]

    report = install_codex(
        CodexOptions(project_dir=project_dir, **options), verifier=verifier, environ={}, home=home
    )
    report.verified = seen
    return report


def config(home: Path) -> Path:
    return home / ".codex" / "config.toml"


def fusion(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text())["mcp_servers"]["fusion"]


def test_a_fresh_install_writes_the_server_with_the_timeouts_and_key_forwarding(
    home: Path, project: Path
) -> None:
    report = run(home, project)
    entry = fusion(config(home))
    assert entry["command"] == "uvx" and entry["args"] == list(SPEC.args)
    # Codex's defaults (10 s start, 60 s per tool call) would kill Fusion before it answers.
    assert entry["startup_timeout_sec"] == STARTUP_TIMEOUT_S >= 60
    assert entry["tool_timeout_sec"] == TOOL_TIMEOUT_S > 90
    assert entry["env_vars"] == list(PROVIDER_KEYS)
    assert report.changed and report.verified == [SPEC]


def test_the_shipped_snippet_is_what_the_installer_writes(home: Path, project: Path) -> None:
    run(home, project, agents_md=False)
    snippet = tomllib.loads(integration_text("codex", "config.toml.snippet"))
    assert snippet["mcp_servers"]["fusion"] == fusion(config(home))


def test_comments_other_servers_and_settings_survive_byte_for_byte(
    home: Path, project: Path
) -> None:
    config(home).write_text(USER_CONFIG)
    run(home, project)
    text = config(home).read_text()
    assert text.startswith(USER_CONFIG)  # nothing the user wrote was reformatted
    data = tomllib.loads(text)
    assert data["model"] == "o4-mini"
    assert data["mcp_servers"]["github"]["tool_timeout_sec"] == 30
    assert "fusion" in data["mcp_servers"]


def test_running_it_twice_changes_nothing_the_second_time(home: Path, project: Path) -> None:
    config(home).write_text(USER_CONFIG)
    run(home, project)
    before = (config(home).read_text(), (home / ".codex" / "AGENTS.md").read_text())
    report = run(home, project)
    assert not report.changed and len(report.unchanged) == 2
    assert (config(home).read_text(), (home / ".codex" / "AGENTS.md").read_text()) == before


def test_a_different_fusion_server_is_refused_without_force_and_nothing_is_written(
    home: Path, project: Path
) -> None:
    text = '[mcp_servers.fusion]\ncommand = "python"\nargs = ["-m", "other"]\nenv = { A = "1" }\n'
    config(home).write_text(text)
    with pytest.raises(InstallError, match="--force"):
        run(home, project)
    assert config(home).read_text() == text and not (home / ".codex" / "AGENTS.md").exists()
    run(home, project, force=True)
    entry = fusion(config(home))
    assert entry["command"] == "uvx" and entry["env"] == {"A": "1"}  # their other keys stay


def test_timeouts_are_raised_when_too_low_and_never_lowered(home: Path, project: Path) -> None:
    config(home).write_text(
        "[mcp_servers.fusion]\ncommand = 'uvx'\n"
        f"args = {list(SPEC.args)!r}\n"
        "startup_timeout_sec = 5\ntool_timeout_sec = 600\nenv_vars = ['OPENAI_API_KEY', 'MINE']\n"
    )
    report = run(home, project, agents_md=False)
    entry = fusion(config(home))
    assert entry["startup_timeout_sec"] == STARTUP_TIMEOUT_S and entry["tool_timeout_sec"] == 600
    assert entry["env_vars"][:2] == ["OPENAI_API_KEY", "MINE"]  # theirs first, ours added
    assert set(PROVIDER_KEYS) <= set(entry["env_vars"])
    assert "was 5" in report.actions[0]


def test_a_malformed_config_is_refused_untouched(home: Path, project: Path) -> None:
    config(home).write_text("[mcp_servers.fusion\ncommand = ")
    with pytest.raises(InstallError, match="not valid TOML"):
        run(home, project)
    assert config(home).read_text() == "[mcp_servers.fusion\ncommand = "
    assert not (home / ".codex" / "AGENTS.md").exists()


def test_mcp_servers_that_is_not_a_table_is_refused(home: Path, project: Path) -> None:
    config(home).write_text("mcp_servers = 3\n")
    with pytest.raises(InstallError, match="not a table"):
        run(home, project)


def test_a_dry_run_writes_nothing(home: Path, project: Path) -> None:
    config(home).write_text(USER_CONFIG)
    report = run(home, project, dry_run=True)
    assert config(home).read_text() == USER_CONFIG and not (home / ".codex" / "AGENTS.md").exists()
    assert report.verified == [] and report.commands
    assert all(a.startswith("Would") for a in report.actions) and len(report.actions) == 2


def test_the_project_scope_uses_the_project_files_and_notes_trust(
    home: Path, project: Path
) -> None:
    report = run(home, project, project=True)
    assert fusion(project / ".codex" / "config.toml")["command"] == "uvx"
    assert (project / "AGENTS.md").exists() and not config(home).exists()
    assert any("trust" in note for note in report.notes)


def test_codex_home_moves_the_user_config(tmp_path: Path, project: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    install_codex(
        CodexOptions(project_dir=project, verify=False),
        environ={"CODEX_HOME": str(elsewhere)},
        home=tmp_path / "unused",
    )
    assert (elsewhere / "config.toml").exists() and (elsewhere / "AGENTS.md").exists()


def test_a_server_that_does_not_start_is_not_registered(home: Path, project: Path) -> None:
    def broken(_spec: ServerSpec) -> list[str]:
        raise InstallError("no start")

    with pytest.raises(InstallError, match="no start"):
        install_codex(CodexOptions(project_dir=project), verifier=broken, environ={}, home=home)
    assert not config(home).exists()


# -- the AGENTS.md block -------------------------------------------------------------------------


def test_the_block_is_appended_after_the_users_text_and_refreshed_in_place() -> None:
    text, state = with_agents_block("# Rules\n\nBe kind.\n")
    assert state == "added" and text.startswith("# Rules\n\nBe kind.\n\n" + BEGIN)
    assert integration_text("codex", "AGENTS.fusion.md").rstrip() in text and text.endswith(
        END + "\n"
    )
    assert with_agents_block(text) == (text, "same")
    stale = text.replace("Be kind.", "Be kind.\n").replace("Fusion:", "Old title:")
    refreshed, state = with_agents_block(stale)
    assert state == "replaced" and "Old title" not in refreshed and "Be kind." in refreshed


def test_text_after_the_block_survives_a_refresh() -> None:
    text, _ = with_agents_block("before\n")
    edited = text.replace("a panel", "EDITED") + "\n## After\nkeep me\n"
    refreshed, state = with_agents_block(edited)
    assert state == "replaced" and refreshed.endswith("## After\nkeep me\n")
    assert refreshed.startswith("before\n")


@pytest.mark.parametrize("damage", ["begin", "end", "swapped"])
def test_a_damaged_block_is_refused(damage: str) -> None:
    text, _ = with_agents_block("x\n")
    if damage == "begin":
        text = text.replace(END, "")
    elif damage == "end":
        text = text.replace(BEGIN, "")
    else:
        text = text.replace(BEGIN, "@@").replace(END, BEGIN).replace("@@", END)
    with pytest.raises(InstallError, match="damaged"):
        with_agents_block(text)


def test_no_agents_md_leaves_the_file_alone(home: Path, project: Path) -> None:
    run(home, project, agents_md=False)
    assert not (home / ".codex" / "AGENTS.md").exists()


# -- doctor hook ---------------------------------------------------------------------------------


def test_inspect_reports_absent_ok_and_the_things_that_make_codex_misbehave(
    home: Path, project: Path
) -> None:
    project_finding, user_finding = inspect_codex(project_dir=project, environ={}, home=home)
    assert project_finding.status == user_finding.status == "absent"
    assert project_finding.fix == "fusion install codex --project"

    run(home, project)
    assert inspect_codex(project_dir=project, environ={}, home=home)[1].status == "ok"

    config(home).write_text(
        "[mcp_servers.fusion]\ncommand = 'uvx'\n"
        "args = ['--from', 'git+x', 'fusion', 'mcp']\nenabled = false\n"
    )
    warned = inspect_codex(project_dir=project, environ={}, home=home)[1]
    assert warned.status == "warn"
    for problem in ("disabled", "tool_timeout_sec", "startup_timeout_sec", "provider key"):
        assert problem in warned.message

    config(home).write_text("[mcp_servers.fusion]\ncommand = 'node'\nargs = ['a.js']\n")
    assert inspect_codex(project_dir=project, environ={}, home=home)[1].status == "warn"
    config(home).write_text("[oops")
    assert inspect_codex(project_dir=project, environ={}, home=home)[1].status == "error"


def test_inspect_clients_lists_all_three_clients(home: Path, project: Path) -> None:
    findings = inspect_clients(project_dir=project, environ={}, home=home)
    assert {f.client for f in findings} == {"claude-code", "cursor", "codex"}
    assert all(f.status == "absent" and f.fix for f in findings)


# -- the command line ----------------------------------------------------------------------------


def test_cli_dry_run_and_install(
    home: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    dry = CliRunner().invoke(app, ["install", "codex", "--dry-run", "--no-verify"])
    assert dry.exit_code == 0, dry.output
    assert "Would change" in dry.output and not config(home).exists()
    done = CliRunner().invoke(app, ["install", "codex", "--no-verify"])
    assert done.exit_code == 0 and fusion(config(home))["command"] == "uvx"
    again = CliRunner().invoke(app, ["install", "codex", "--no-verify"])
    assert again.exit_code == 0 and "nothing to do" in again.output


def test_cli_refuses_a_malformed_config_with_exit_code_one(
    home: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    config(home).write_text("[")
    result = CliRunner().invoke(app, ["install", "codex", "--no-verify"])
    assert result.exit_code == 1 and "not valid TOML" in result.output


def test_inspect_clients_sees_a_registered_claude_code_server(home: Path, project: Path) -> None:
    (project / ".mcp.json").write_text(
        '{"mcpServers": {"fusion": {"command": "uvx", "args": ["fusion", "mcp"]}}}'
    )
    (home / ".claude.json").write_text("{")
    by_client = {
        (f.client, f.status) for f in inspect_clients(project_dir=project, environ={}, home=home)
    }
    assert ("claude-code", "ok") in by_client and ("claude-code", "error") in by_client
