"""``fusion install claude-code``: register the Fusion MCP server (or the plugin) with Claude Code.

Two ways in, both idempotent and both checked by starting the server and listing its tools:

* the MCP server alone: ``claude mcp add`` for the user scope, or an edit of ``.mcp.json`` for the
  project scope (that file is documented and shared with the team, so it is merged, never
  overwritten);
* the plugin (``--plugin``): the marketplace entry and the plugin, which bring the server, the
  ``/fusion:*`` commands, the skills and the ``fusion-advisor`` subagent.

Claude Code owns ``~/.claude.json`` and rewrites it often, so that file is only read (to see what
is already registered); every change to it goes through the ``claude`` command.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fusion.install.common import (
    REPO_SLUG,
    SERVER_NAME,
    InstallError,
    InstallReport,
    ServerSpec,
    checkout_spec,
    merge_server,
    read_json_object,
    shell_quote,
    uvx_spec,
    verify_server,
)

Scope = Literal["user", "project"]
MARKETPLACE_NAME = "fusion-code-orchestrator"
PLUGIN_NAME = "fusion"
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
CLAUDE = "claude"  # resolved through PATH when the command runs
COMMAND_TIMEOUT_S = 180.0  # `claude plugin marketplace add` clones a repository

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
Verifier = Callable[[ServerSpec], list[str]]


def run_command(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one command and capture its output (the default ``Runner``)."""
    return subprocess.run(
        list(args), capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S, check=False
    )


@dataclass
class ClaudeCodeOptions:
    scope: Scope = "user"
    plugin: bool = False  # install the plugin instead of the bare MCP server
    ref: str | None = None  # pin the uvx source to a tag, branch or commit
    checkout: Path | None = None  # run from a local clone instead of GitHub
    dry_run: bool = False
    force: bool = False  # replace a different 'fusion' server that is already registered
    verify: bool = True
    project_dir: Path | None = None  # where project scope writes .mcp.json (default: cwd)


def install_claude_code(
    options: ClaudeCodeOptions,
    *,
    runner: Runner = run_command,
    verifier: Verifier = verify_server,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> InstallReport:
    """Install, or with ``dry_run`` only report what would happen. Raises ``InstallError``."""
    env = os.environ if environ is None else environ
    report = InstallReport(dry_run=options.dry_run)
    if options.plugin:
        if options.ref or options.checkout:
            msg = (
                "--ref and --local-checkout choose where the bare MCP server comes from; the "
                "plugin brings its own server definition. Drop them, or drop --plugin."
            )
            raise InstallError(msg)
        spec = uvx_spec()
    else:
        spec = checkout_spec(options.checkout) if options.checkout else uvx_spec(options.ref)

    claude = which("claude")
    needs_claude = options.plugin or options.scope == "user"
    if needs_claude and claude is None and not options.dry_run:
        raise InstallError(_no_claude_message(options, spec))
    if needs_claude and claude is None:
        report.notes.append("The `claude` command is not on PATH; a real run would stop here.")

    if options.verify and not options.dry_run:
        names = verifier(spec)
        report.checks.append(f"The server starts and lists {len(names)} tools")
    elif options.verify:
        report.commands.append(f"(check) start `{spec.shell()}` and list its tools")

    if options.plugin:
        _install_plugin(options, report, runner, claude is not None, spec)
    elif options.scope == "project":
        _install_project(options, report, spec)
    else:
        _install_user(options, report, runner, spec, env, home)
    report.notes.append(
        "Restart Claude Code (or run /reload-plugins / /mcp), then ask it to call fusion_stats."
    )
    report.notes.append(
        "Provider keys come from the environment Claude Code starts in, or a .env file in the "
        "project directory: ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY."
    )
    return report


def _no_claude_message(options: ClaudeCodeOptions, spec: ServerSpec) -> str:
    if options.plugin:
        by_hand = (
            f"  claude plugin marketplace add {REPO_SLUG}\n  claude plugin install {PLUGIN_ID}"
        )
    else:
        by_hand = f"  claude mcp add --scope user {SERVER_NAME} -- {spec.shell()}"
    return (
        "The `claude` command is not on PATH, so Claude Code cannot be configured from here. "
        f"Install Claude Code, or run this yourself:\n{by_hand}"
    )


# -- MCP server alone -------------------------------------------------------------------------


def _install_project(options: ClaudeCodeOptions, report: InstallReport, spec: ServerSpec) -> None:
    path = (options.project_dir or Path.cwd()) / ".mcp.json"
    outcome = merge_server(path, spec, force=options.force, dry_run=options.dry_run)
    if outcome == "unchanged":
        report.unchanged.append(f"{path} already registers '{SERVER_NAME}'")
        return
    verb = "Would write" if options.dry_run else "Wrote"
    report.actions.append(f"{verb} the '{SERVER_NAME}' server to {path} ({outcome})")
    report.notes.append(
        "Claude Code asks once to approve a project .mcp.json server; approve 'fusion' in /mcp."
    )


def _install_user(
    options: ClaudeCodeOptions,
    report: InstallReport,
    runner: Runner,
    spec: ServerSpec,
    env: Mapping[str, str],
    home: Path | None,
) -> None:
    existing = _registered_user_server(env, home)
    if existing is not None and spec.matches(existing):
        report.unchanged.append(f"'{SERVER_NAME}' is already registered for your user")
        return
    commands: list[list[str]] = []
    if existing is not None:
        if not options.force:
            msg = (
                f"A different '{SERVER_NAME}' server is already registered for your user "
                f"({_entry_text(existing)}). Run again with --force to replace it."
            )
            raise InstallError(msg)
        commands.append([CLAUDE, "mcp", "remove", SERVER_NAME, "--scope", "user"])
    commands.append(
        [CLAUDE, "mcp", "add", "--scope", "user", SERVER_NAME, "--", spec.command, *spec.args]
    )
    _run_all(options, report, runner, commands)
    verb = "Would register" if options.dry_run else "Registered"
    report.actions.append(f"{verb} the '{SERVER_NAME}' server for your user in Claude Code")


def _registered_user_server(env: Mapping[str, str], home: Path | None) -> object | None:
    """The 'fusion' entry in Claude Code's user config, read-only; None when there is none."""
    config_dir = Path(env["CLAUDE_CONFIG_DIR"]) if env.get("CLAUDE_CONFIG_DIR") else None
    path = (config_dir or home or Path.home()) / ".claude.json"
    servers = read_json_object(path).get("mcpServers", {})
    return servers.get(SERVER_NAME) if isinstance(servers, dict) else None


def _entry_text(entry: object) -> str:
    if isinstance(entry, dict) and "command" in entry:
        return " ".join([str(entry["command"]), *map(str, entry.get("args", []))])
    return json.dumps(entry)


# -- the plugin -------------------------------------------------------------------------------


def _install_plugin(
    options: ClaudeCodeOptions,
    report: InstallReport,
    runner: Runner,
    have_claude: bool,
    spec: ServerSpec,
) -> None:
    del spec  # the plugin's own .mcp.json defines the server; it was only launch-checked
    marketplaces = _marketplaces(runner, have_claude)
    plugins = _installed_plugins(runner, have_claude)
    commands: list[list[str]] = []
    if MARKETPLACE_NAME in marketplaces:
        report.unchanged.append(f"marketplace '{MARKETPLACE_NAME}' is already added")
    else:
        commands.append([CLAUDE, "plugin", "marketplace", "add", REPO_SLUG])
    scopes = plugins.get(PLUGIN_ID, set())
    if options.scope in scopes:
        report.unchanged.append(
            f"plugin {PLUGIN_ID} is already installed for scope {options.scope}"
        )
    else:
        commands.append([CLAUDE, "plugin", "install", PLUGIN_ID, "--scope", options.scope])
    _run_all(options, report, runner, commands)
    if commands:
        verb = "Would install" if options.dry_run else "Installed"
        report.actions.append(f"{verb} the Fusion plugin ({PLUGIN_ID}, scope {options.scope})")
    report.notes.append(
        "The plugin starts its own 'fusion' server. If you also registered one with "
        "`fusion install claude-code` (without --plugin), remove it: `claude mcp remove fusion`."
    )


def _json_listing(runner: Runner, args: Sequence[str]) -> list[dict[str, Any]]:
    done = runner(args)
    try:
        data = json.loads(done.stdout)
    except json.JSONDecodeError:
        data = None
    if done.returncode != 0 or not isinstance(data, list):
        output = done.stderr.strip() or done.stdout.strip()
        msg = f"Could not read the output of `{' '.join(args)}`: {output}"
        raise InstallError(msg)
    return [item for item in data if isinstance(item, dict)]


def _marketplaces(runner: Runner, have_claude: bool) -> set[str]:
    """Names of the configured marketplaces. Without ``claude`` (a dry run only) none are known,
    so the dry run shows every step."""
    if not have_claude:
        return set()
    listing = _json_listing(runner, [CLAUDE, "plugin", "marketplace", "list", "--json"])
    return {str(item.get("name")) for item in listing}


def _installed_plugins(runner: Runner, have_claude: bool) -> dict[str, set[str]]:
    if not have_claude:
        return {}
    found: dict[str, set[str]] = {}
    for item in _json_listing(runner, [CLAUDE, "plugin", "list", "--json"]):
        found.setdefault(str(item.get("id")), set()).add(str(item.get("scope")))
    return found


# -- running the commands ---------------------------------------------------------------------


def _run_all(
    options: ClaudeCodeOptions,
    report: InstallReport,
    runner: Runner,
    commands: list[list[str]],
) -> None:
    for command in commands:
        text = " ".join(shell_quote(part) for part in command)
        report.commands.append(text)
        if options.dry_run:
            continue
        done = runner(command)
        if done.returncode != 0:
            detail = done.stderr.strip() or done.stdout.strip() or f"exit code {done.returncode}"
            msg = f"`{text}` failed: {detail}"
            raise InstallError(msg)
