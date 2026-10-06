"""`fusion install claude-code`: idempotent, dry-runnable, and careful with existing config.

Claude Code's `claude` command is replaced by a fake that keeps its state in a temporary HOME, so
nothing here touches the real configuration.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fusion.cli.app import app
from fusion.install.claude_code import (
    MARKETPLACE_NAME,
    PLUGIN_ID,
    ClaudeCodeOptions,
    install_claude_code,
)
from fusion.install.common import (
    InstallError,
    ServerSpec,
    checkout_spec,
    merge_server,
    uvx_spec,
    verify_server,
)

SPEC = uvx_spec()


class FakeClaude:
    """The parts of the `claude` command the installer uses, backed by files under ``home``."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.calls: list[list[str]] = []
        self.fail: dict[str, str] = {}  # a command word -> the error it returns

    @property
    def config(self) -> Path:
        return self.home / ".claude.json"

    def state(self) -> dict[str, Any]:
        return json.loads(self.config.read_text()) if self.config.exists() else {}

    def save(self, state: dict[str, Any]) -> None:
        self.config.write_text(json.dumps(state))

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        words = list(args)
        self.calls.append(words)
        for word, error in self.fail.items():
            if word in words:
                return subprocess.CompletedProcess(words, 1, "", error)
        state = self.state()
        match words[1:]:
            case ["mcp", "add", "--scope", "user", name, "--", command, *rest]:
                state.setdefault("mcpServers", {})[name] = {
                    "type": "stdio",
                    "command": command,
                    "args": rest,
                    "env": {},
                }
            case ["mcp", "remove", name, "--scope", "user"]:
                state.get("mcpServers", {}).pop(name, None)
            case ["plugin", "marketplace", "list", "--json"]:
                listing = [{"name": n} for n in state.get("marketplaces", [])]
                return subprocess.CompletedProcess(words, 0, json.dumps(listing), "")
            case ["plugin", "marketplace", "add", _source]:
                state.setdefault("marketplaces", []).append(MARKETPLACE_NAME)
            case ["plugin", "list", "--json"]:
                return subprocess.CompletedProcess(
                    words, 0, json.dumps(state.get("plugins", [])), ""
                )
            case ["plugin", "install", plugin_id, "--scope", scope]:
                state.setdefault("plugins", []).append({"id": plugin_id, "scope": scope})
            case _:
                return subprocess.CompletedProcess(words, 2, "", f"unexpected: {words}")
        self.save(state)
        return subprocess.CompletedProcess(words, 0, "", "")

    def mutations(self) -> list[list[str]]:
        reads = (["--json"], ["list"])
        return [c for c in self.calls if not any(c[-len(r) :] == r for r in reads)]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def claude(home: Path) -> FakeClaude:
    return FakeClaude(home)


def run(
    claude: FakeClaude,
    home: Path,
    *,
    verifier: Any = None,
    which: Any = lambda _name: "/usr/bin/claude",
    **options: Any,
) -> Any:
    seen: list[ServerSpec] = []

    def default_verifier(spec: ServerSpec) -> list[str]:
        seen.append(spec)
        return ["fusion_ask", "fusion_stats"]

    report = install_claude_code(
        ClaudeCodeOptions(**options),
        runner=claude,
        verifier=verifier or default_verifier,
        which=which,
        environ={},
        home=home,
    )
    report.verified = seen  # type: ignore[attr-defined]
    return report


# -- the command that is registered --------------------------------------------------------------


def test_the_server_runs_from_github_with_uvx_and_can_be_pinned() -> None:
    assert SPEC.shell() == (
        "uvx --python '>=3.12' --managed-python "
        "--from git+https://github.com/alexandre0sheva/fusion-code-orchestrator fusion mcp"
    )
    assert uvx_spec("v0.2.0").args[-3].endswith("fusion-code-orchestrator@v0.2.0")


def test_the_server_always_gets_a_python_it_can_run_on() -> None:
    """Without this uvx picks the machine's default Python (3.9 on a stock Mac) and the server
    dies at start-up with nobody watching."""
    assert SPEC.args[:3] == ("--python", ">=3.12", "--managed-python")


def test_a_local_checkout_runs_with_uv_and_must_be_a_checkout(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    spec = checkout_spec(root)
    assert spec.command == "uv" and spec.args[:3] == ("run", "--directory", str(root))
    with pytest.raises(InstallError, match="not a Fusion checkout"):
        checkout_spec(tmp_path)


# -- user scope ----------------------------------------------------------------------------------


def test_user_scope_registers_the_server_through_claude_mcp_add(
    claude: FakeClaude, home: Path
) -> None:
    report = run(claude, home)
    assert claude.state()["mcpServers"]["fusion"]["args"] == list(SPEC.args)
    assert claude.mutations() == [
        ["claude", "mcp", "add", "--scope", "user", "fusion", "--", "uvx", *SPEC.args]
    ]
    assert report.verified == [SPEC] and report.changed


def test_running_it_twice_changes_nothing_the_second_time(claude: FakeClaude, home: Path) -> None:
    run(claude, home)
    before = claude.config.read_text()
    claude.calls.clear()
    report = run(claude, home)
    assert not report.changed and report.unchanged and claude.calls == []
    assert claude.config.read_text() == before


def test_a_different_fusion_server_is_never_replaced_without_force(
    claude: FakeClaude, home: Path
) -> None:
    claude.save({"mcpServers": {"fusion": {"command": "uv", "args": ["run", "fusion", "mcp"]}}})
    with pytest.raises(InstallError, match="--force"):
        run(claude, home)
    assert claude.calls == []
    run(claude, home, force=True)
    assert [c[1:3] for c in claude.mutations()] == [["mcp", "remove"], ["mcp", "add"]]
    assert claude.state()["mcpServers"]["fusion"]["command"] == "uvx"


def test_other_servers_survive(claude: FakeClaude, home: Path) -> None:
    claude.save({"mcpServers": {"github": {"command": "gh-mcp", "args": []}}, "theme": "dark"})
    run(claude, home)
    state = claude.state()
    assert set(state["mcpServers"]) == {"github", "fusion"} and state["theme"] == "dark"


def test_claude_config_dir_moves_where_the_existing_server_is_looked_for(
    claude: FakeClaude, home: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".claude.json").write_text(json.dumps({"mcpServers": {"fusion": SPEC.as_entry()}}))
    report = install_claude_code(
        ClaudeCodeOptions(),
        runner=claude,
        verifier=lambda _spec: ["fusion_ask"],
        which=lambda _name: "claude",
        environ={"CLAUDE_CONFIG_DIR": str(elsewhere)},
        home=home,
    )
    assert report.unchanged and claude.calls == []


def test_a_failing_claude_command_is_reported_with_its_error(
    claude: FakeClaude, home: Path
) -> None:
    claude.fail["add"] = "permission denied"
    with pytest.raises(InstallError, match="permission denied"):
        run(claude, home)


def test_without_the_claude_command_the_error_gives_the_command_to_run(
    claude: FakeClaude, home: Path
) -> None:
    with pytest.raises(InstallError, match=r"claude mcp add --scope user fusion -- uvx"):
        run(claude, home, which=lambda _name: None)


# -- the launch check ----------------------------------------------------------------------------


def test_a_server_that_does_not_start_is_not_registered(claude: FakeClaude, home: Path) -> None:
    def broken(_spec: ServerSpec) -> list[str]:
        raise InstallError("Could not start the server")

    with pytest.raises(InstallError, match="Could not start"):
        run(claude, home, verifier=broken)
    assert claude.calls == [] and not claude.config.exists()


def test_the_check_can_be_skipped(claude: FakeClaude, home: Path) -> None:
    def never(_spec: ServerSpec) -> list[str]:
        raise AssertionError("must not run")

    run(claude, home, verifier=never, verify=False)
    assert claude.state()["mcpServers"]["fusion"]


def test_the_check_really_starts_the_server_and_lists_its_tools() -> None:
    spec = ServerSpec(sys.executable, ("-m", "fusion.main", "mcp"))
    tools = verify_server(spec, timeout=60)
    assert {"fusion_ask", "fusion_review_diff", "fusion_stats"} <= set(tools)


def test_the_check_reports_a_command_that_cannot_run() -> None:
    with pytest.raises(InstallError, match="Could not start"):
        verify_server(ServerSpec("fusion-no-such-command", ()), timeout=20)


# -- dry run -------------------------------------------------------------------------------------


def test_a_dry__run_shows_the_command_and_changes_nothing(claude: FakeClaude, home: Path) -> None:
    report = run(claude, home, dry_run=True)
    assert claude.calls == [] and not claude.config.exists()
    assert report.dry_run and report.verified == []  # type: ignore[attr-defined]
    assert any("mcp add --scope user fusion -- uvx" in c for c in report.commands)


# -- project scope -------------------------------------------------------------------------------


def test_project_scope_merges_into_mcp_json_without_claude(
    claude: FakeClaude, home: Path, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = project / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"db": {"command": "db-mcp"}}, "note": 1}))
    run(claude, home, scope="project", project_dir=project, which=lambda _n: None)
    data = json.loads(path.read_text())
    assert set(data["mcpServers"]) == {"db", "fusion"} and data["note"] == 1
    assert data["mcpServers"]["fusion"]["args"] == list(SPEC.args)
    assert claude.calls == []
    before = path.read_text()
    report = run(claude, home, scope="project", project_dir=project, which=lambda _n: None)
    assert not report.changed and path.read_text() == before


def test_project_scope_refuses_a_malformed_file_and_leaves_it_alone(
    claude: FakeClaude, home: Path, tmp_path: Path
) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text("{ not json")
    with pytest.raises(InstallError, match="not valid JSON"):
        run(claude, home, scope="project", project_dir=tmp_path)
    assert path.read_text() == "{ not json"


def test_merge_server_keeps_extra_keys_of_the_entry_it_replaces(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"fusion": {"command": "old", "timeout": 5}}}))
    with pytest.raises(InstallError, match="different 'fusion' server"):
        merge_server(path, SPEC)
    assert merge_server(path, SPEC, force=True) == "replaced"
    entry = json.loads(path.read_text())["mcpServers"]["fusion"]
    assert entry["command"] == "uvx" and entry["timeout"] == 5


def test_merge_server_dry_run_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    assert merge_server(path, SPEC, dry_run=True) == "added" and not path.exists()


def test_merge_server_refuses_a_config_that_is_not_an_object(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text("[]")
    with pytest.raises(InstallError, match="JSON object"):
        merge_server(path, SPEC)
    path.write_text(json.dumps({"mcpServers": []}))
    with pytest.raises(InstallError, match="'mcpServers' is not an object"):
        merge_server(path, SPEC)


# -- the plugin ----------------------------------------------------------------------------------


def test_the_plugin_is_installed_from_the_marketplace(claude: FakeClaude, home: Path) -> None:
    run(claude, home, plugin=True)
    assert claude.mutations() == [
        ["claude", "plugin", "marketplace", "add", "alexandre0sheva/fusion-code-orchestrator"],
        ["claude", "plugin", "install", PLUGIN_ID, "--scope", "user"],
    ]


def test_the_plugin_install_is_idempotent_and_scope_aware(claude: FakeClaude, home: Path) -> None:
    run(claude, home, plugin=True)
    claude.calls.clear()
    assert not run(claude, home, plugin=True).changed and claude.mutations() == []
    run(claude, home, plugin=True, scope="project")
    assert claude.mutations() == [["claude", "plugin", "install", PLUGIN_ID, "--scope", "project"]]


def test_a_dry_run_of_the_plugin_shows_both_steps(claude: FakeClaude, home: Path) -> None:
    report = run(claude, home, plugin=True, dry_run=True)
    assert len(report.commands) == 3  # the launch check, then add and install
    assert claude.mutations() == []


def test_the_plugin_brings_its_own_server_definition(claude: FakeClaude, home: Path) -> None:
    for extra in ({"ref": "v1"}, {"checkout": Path("/x")}):
        with pytest.raises(InstallError, match="plugin brings its own server"):
            run(claude, home, plugin=True, **extra)


def test_unreadable_plugin_listings_stop_the_install(claude: FakeClaude, home: Path) -> None:
    claude.fail["list"] = "boom"
    with pytest.raises(InstallError, match="Could not read the output"):
        run(claude, home, plugin=True)


# -- the command line ----------------------------------------------------------------------------


def test_cli_dry_run_for_project_scope_prints_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app, ["install", "claude-code", "--scope", "project", "--dry-run", "--no-verify"]
    )
    assert result.exit_code == 0, result.output
    assert "Would write" in result.output and not (tmp_path / ".mcp.json").exists()


def test_cli_dry_run_without_claude_still_prints_the_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = CliRunner().invoke(
        app, ["install", "claude-code", "--plugin", "--dry-run", "--no-verify"]
    )
    assert result.exit_code == 0, result.output
    assert "claude plugin install fusion@fusion-code-orchestrator" in result.output
    assert "not on PATH" in result.output


def test_cli_reports_an_install_error_and_exits_with_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".mcp.json").write_text("{")
    result = CliRunner().invoke(
        app, ["install", "claude-code", "--scope", "project", "--no-verify"]
    )
    assert result.exit_code == 1 and "not valid JSON" in result.output


def test_cli_rejects_an_unknown_scope() -> None:
    result = CliRunner().invoke(app, ["install", "claude-code", "--scope", "galaxy"])
    assert result.exit_code != 0
