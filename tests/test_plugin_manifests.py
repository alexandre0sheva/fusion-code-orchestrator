"""The Claude Code plugin and marketplace: manifests, components and what they point at.

Claude Code's own validator is run too when the `claude` command is installed (it reads local
files only), so a schema change in Claude Code shows up here instead of at install time.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

from fusion.install.claude_code import MARKETPLACE_NAME, PLUGIN_ID, PLUGIN_NAME
from fusion.install.common import REPO_SLUG, uvx_spec
from fusion.mcp_server.server import create_mcp_server

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"
FIXTURES = Path(__file__).parent / "fixtures"
TOOL_PREFIX = f"mcp__plugin_{PLUGIN_NAME}_fusion__"
COMMANDS = {
    "ask",
    "review",
    "debug",
    "plan",
    "decide",
    "stats",
    "bench",
    "eval",
    "ab",
    "digest",
    "patch",
    "feedback",
}
SKILLS = {
    "fusion-orchestrator",
    "fusion-review",
    "fusion-debug",
    "fusion-decide",
    "fusion-plan",
    "fusion-eval",
    "fusion-patch",
}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    assert match, f"{path} has no YAML frontmatter"
    meta = yaml.safe_load(match.group(1))
    assert isinstance(meta, dict), path
    return meta, match.group(2)


async def _server_tools(tmp_path: Path) -> set[str]:
    from fastmcp import Client

    async with Client(create_mcp_server(db_path=str(tmp_path / "r.db"))) as client:
        return {tool.name for tool in await client.list_tools()}


# -- manifests --------------------------------------------------------------------------------


def test_plugin_manifest_matches_the_documented_schema() -> None:
    schema = _json(FIXTURES / "claude_plugin_manifest.schema.json")
    manifest = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    jsonschema.validate(manifest, schema)
    assert manifest["name"] == PLUGIN_NAME  # the name is the command prefix: /fusion:ask


def test_the_manifest_schema_fixture_rejects_what_claude_code_rejects() -> None:
    schema = _json(FIXTURES / "claude_plugin_manifest.schema.json")
    for bad in (
        {"id": "x", "name": "fusion"},  # the old, unrecognised key
        {"name": "Fusion Code"},  # not kebab-case
        {"name": "fusion", "author": "someone"},  # author is an object
        {"version": "1"},  # no name
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)


def test_marketplace_matches_the_documented_schema_and_points_at_the_plugin() -> None:
    schema = _json(FIXTURES / "claude_marketplace.schema.json")
    marketplace = _json(ROOT / ".claude-plugin" / "marketplace.json")
    jsonschema.validate(marketplace, schema)
    assert marketplace["name"] == MARKETPLACE_NAME
    (entry,) = marketplace["plugins"]
    manifest = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    assert entry["name"] == manifest["name"]  # install ids use the entry name
    assert (ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file()
    assert f"{entry['name']}@{marketplace['name']}" == PLUGIN_ID


def test_the_plugin_starts_the_same_server_the_installer_registers() -> None:
    config = _json(PLUGIN / ".mcp.json")
    server = config["mcpServers"]["fusion"]
    spec = uvx_spec()
    assert server["command"] == spec.command and server["args"] == list(spec.args)
    assert any(REPO_SLUG in arg for arg in server["args"])
    assert "cwd" not in server and "${workspaceFolder}" not in json.dumps(config)


def test_the_plugin_version_is_the_package_version() -> None:
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert (
        _json(PLUGIN / ".claude-plugin" / "plugin.json")["version"]
        == pyproject["project"]["version"]
    )


# -- components -------------------------------------------------------------------------------


async def test_commands_are_the_documented_set_and_call_real_tools(tmp_path: Path) -> None:
    tools = await _server_tools(tmp_path)
    files = {p.stem: p for p in (PLUGIN / "commands").glob("*.md")}
    assert set(files) == COMMANDS
    for name, path in files.items():
        meta, body = _frontmatter(path)
        assert str(meta.get("description", "")).strip(), name
        allowed = str(meta.get("allowed-tools", "")).split()
        for item in allowed:
            if item.startswith("mcp__"):
                assert item.startswith(TOOL_PREFIX), (name, item)
                assert item.removeprefix(TOOL_PREFIX) in tools, (name, item)
        for mentioned in re.findall(r"`(fusion_[a-z_]+)`", body):
            assert mentioned in tools, (name, mentioned)
    # a command that takes input says where it goes
    assert all("$ARGUMENTS" in files[n].read_text() for n in ("ask", "review", "plan", "decide"))


async def test_skills_say_when_to_use_them_and_when_not_to(tmp_path: Path) -> None:
    tools = await _server_tools(tmp_path)
    found = {p.parent.name: p for p in (PLUGIN / "skills").glob("*/SKILL.md")}
    assert set(found) == SKILLS
    for name, path in found.items():
        meta, body = _frontmatter(path)
        assert meta["name"] == name
        description = str(meta["description"])
        assert description.startswith("Use ") and "Do not use" in description, name
        assert len(description) <= 1536, name  # Claude Code truncates the listing here
        assert meta.get("user-invocable") is False, name  # the /fusion:* commands are the entry
        for mentioned in re.findall(r"`(fusion_[a-z_]+)`", body):
            assert mentioned in tools, (name, mentioned)


async def test_the_advisor_subagent_is_confined_to_reading_and_fusion(tmp_path: Path) -> None:
    tools = await _server_tools(tmp_path)
    meta, body = _frontmatter(PLUGIN / "agents" / "fusion-advisor.md")
    assert meta["name"] == "fusion-advisor" and "Do not use" in meta["description"]
    granted = {t.strip() for t in str(meta["tools"]).split(",")}
    assert granted == {"Read", "Grep", "Glob", f"mcp__plugin_{PLUGIN_NAME}_fusion"}  # no Bash, Edit
    # fields Claude Code ignores in a plugin agent would mislead the reader
    assert not {"permissionMode", "hooks", "mcpServers", "initialPrompt"} & set(meta)
    for mentioned in re.findall(r"`(fusion_[a-z_]+)`", body):
        assert mentioned in tools, mentioned


async def test_the_advisor_runs_on_haiku_5_5_and_calls_only_listed_tools(tmp_path: Path) -> None:
    """Roadmap Task 17: the advisor only relays and checks, so it runs on the cheap model."""
    meta, body = _frontmatter(PLUGIN / "agents" / "fusion-advisor.md")
    assert meta["model"] == "claude-haiku-5-5"  # the catalog's ID, not an alias that may move
    listed = await _server_tools(tmp_path)
    assert "fusion_plan_feature" not in body and "fusion_decide_architecture" not in body
    assert "fusion_plan" in listed and "`fusion_plan`" in body


async def test_nothing_in_the_plugin_or_client_rules_names_a_tool_that_is_not_listed(
    tmp_path: Path,
) -> None:
    """The 0.2.0 plan and decide tools are hidden aliases: what we ship must call fusion_plan."""
    listed = await _server_tools(tmp_path)
    files = [
        *PLUGIN.rglob("*.md"),
        ROOT / "integrations" / "codex" / "AGENTS.fusion.md",
        ROOT / "integrations" / "cursor" / "rules" / "fusion.mdc",
    ]
    for path in files:
        for mentioned in re.findall(r"`(fusion_[a-z_]+)`", path.read_text(encoding="utf-8")):
            assert mentioned in listed, (path.name, mentioned)
        text = path.read_text(encoding="utf-8")
        assert not {"fusion_plan_feature", "fusion_decide_architecture"} & set(
            re.findall(r"fusion_[a-z_]+", text)
        ), path.name


def test_feedback_command_sends_the_run_id_and_the_outcome() -> None:
    meta, body = _frontmatter(PLUGIN / "commands" / "feedback.md")
    assert str(meta["allowed-tools"]).split() == [f"{TOOL_PREFIX}fusion_feedback"]
    assert "$ARGUMENTS" in body
    for field in ("`run_id`", "`outcome`", "`note`", "used", "partly", "wrong", "ignored"):
        assert field in body, field


def test_plugin_tells_the_host_to_name_files_instead_of_pasting_them() -> None:
    """Roadmap Task 5: Fusion reads `paths` and `diff_ref` itself, so the host saves tokens."""
    texts = {
        p.relative_to(PLUGIN).as_posix(): p.read_text(encoding="utf-8")
        for p in [
            *PLUGIN.glob("commands/*.md"),
            *PLUGIN.glob("skills/*/SKILL.md"),
            *PLUGIN.glob("agents/*.md"),
        ]
    }
    for name in ("commands/review.md", "skills/fusion-review/SKILL.md", "agents/fusion-advisor.md"):
        assert "`diff_ref`" in texts[name], name
    for name in (
        "commands/ask.md",
        "commands/debug.md",
        "skills/fusion-orchestrator/SKILL.md",
        "skills/fusion-debug/SKILL.md",
        "commands/digest.md",
        "commands/patch.md",
        "skills/fusion-patch/SKILL.md",
    ):
        assert "`paths`" in texts[name], name
    assert all("reads no files" not in text for text in texts.values())
    assert "`fusion_digest`" in texts["skills/fusion-orchestrator/SKILL.md"]


def test_the_old_layout_is_gone() -> None:
    assert not (PLUGIN / "plugin.json").exists()  # Claude Code reads .claude-plugin/plugin.json
    assert not (PLUGIN / "mcp").exists()  # the server definition is .mcp.json
    old_commands = re.compile(r"/fusion-(?:ask|review|debug|plan|decide|eval|ab|stats)\b")
    for path in [*PLUGIN.rglob("*.md"), ROOT / "README.md", *(ROOT / "docs").glob("*.md")]:
        assert not old_commands.search(path.read_text(encoding="utf-8")), path


# -- Claude Code's own validator --------------------------------------------------------------


@pytest.mark.skipif(shutil.which("claude") is None, reason="the claude command is not installed")
@pytest.mark.parametrize("target", ["plugin", "."])
def test_claude_plugin_validate_accepts_the_plugin_and_marketplace(target: str) -> None:
    done = subprocess.run(
        ["claude", "plugin", "validate", str(ROOT / target), "--strict"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
