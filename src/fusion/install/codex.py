"""``fusion install codex``: register the Fusion MCP server (and an AGENTS.md block) with Codex.

Codex reads ``[mcp_servers.<name>]`` tables from ``~/.codex/config.toml`` (or ``.codex/config.toml``
in a trusted project). The file is the user's own and is full of their settings and comments, so it
is edited with ``tomlkit``, which keeps everything it does not touch byte for byte, and only after
a check that nothing in it forbids the change. Besides the command, three keys matter for Fusion:

* ``startup_timeout_sec`` (Codex default 10): the first start clones and builds Fusion;
* ``tool_timeout_sec`` (Codex default 60): a call takes tens of seconds and Fusion's own soft limit
  is 90, so Codex's default would abort the call before Fusion answers;
* ``env_vars``: the variables Codex forwards to the server; without the provider keys listed there
  the server starts with no keys.

An existing value that is already at least as large is never lowered.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.toml_document import TOMLDocument

from fusion.install.common import (
    SERVER_NAME,
    ClientCheck,
    InstallError,
    InstallReport,
    ServerSpec,
    checkout_spec,
    integration_text,
    is_fusion_entry,
    uvx_spec,
    verify_server,
    write_text_atomic,
)

Verifier = Callable[[ServerSpec], list[str]]
STARTUP_TIMEOUT_S = 120
TOOL_TIMEOUT_S = 180
PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY")
BEGIN = "<!-- fusion:begin (managed by `fusion install codex`; edits inside are replaced) -->"
END = "<!-- fusion:end -->"
_BLOCK = re.compile(rf"{re.escape(BEGIN)}.*?{re.escape(END)}\n?", re.DOTALL)


@dataclass
class CodexOptions:
    project: bool = False  # .codex/config.toml and ./AGENTS.md instead of the user's ~/.codex
    agents_md: bool = True  # also add the "when to call Fusion" block to AGENTS.md
    ref: str | None = None  # pin the uvx source to a tag, branch or commit
    checkout: Path | None = None  # run from a local clone instead of GitHub
    dry_run: bool = False
    force: bool = False  # replace a different 'fusion' server's command
    verify: bool = True
    project_dir: Path | None = None  # the project (default: cwd)


def codex_home(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else (home or Path.home()) / ".codex"


def config_path(*, project: bool, project_dir: Path | None, codex_dir: Path) -> Path:
    return (
        (project_dir or Path.cwd()) / ".codex" / "config.toml"
        if project
        else codex_dir / "config.toml"
    )


def agents_path(*, project: bool, project_dir: Path | None, codex_dir: Path) -> Path:
    return (project_dir or Path.cwd()) / "AGENTS.md" if project else codex_dir / "AGENTS.md"


def install_codex(
    options: CodexOptions,
    *,
    verifier: Verifier = verify_server,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> InstallReport:
    """Install, or with ``dry_run`` only report what would happen. Raises ``InstallError``."""
    report = InstallReport(dry_run=options.dry_run)
    spec = checkout_spec(options.checkout) if options.checkout else uvx_spec(options.ref)
    codex_dir = codex_home(environ, home)
    config = config_path(
        project=options.project, project_dir=options.project_dir, codex_dir=codex_dir
    )
    agents = agents_path(
        project=options.project, project_dir=options.project_dir, codex_dir=codex_dir
    )

    # Everything that can refuse is computed in memory first, so a refusal changes no file.
    document = load_toml(config)
    changes = apply_server(document, spec, force=options.force)
    block: tuple[str, str] | None = None
    if options.agents_md:
        current = agents.read_text(encoding="utf-8") if agents.exists() else ""
        block = with_agents_block(current)

    if options.verify and not options.dry_run:
        names = verifier(spec)
        report.checks.append(f"The server starts and lists {len(names)} tools")
    elif options.verify:
        report.commands.append(f"(check) start `{spec.shell()}` and list its tools")

    if changes:
        if not options.dry_run:
            write_text_atomic(config, tomlkit.dumps(document))
        report.actions.append(
            f"{'Would change' if options.dry_run else 'Changed'} {config}: {'; '.join(changes)}"
        )
    else:
        report.unchanged.append(f"{config} already registers '{SERVER_NAME}' as needed")

    if block is not None:
        text, state = block
        if state == "same":
            report.unchanged.append(f"{agents} already has the Fusion block")
        else:
            if not options.dry_run:
                write_text_atomic(agents, text)
            report.actions.append(
                f"{'Would write' if options.dry_run else 'Wrote'} the Fusion block to {agents} "
                f"({state})"
            )

    if options.project:
        report.notes.append(
            "Codex reads a project's .codex/config.toml only after you trust the project."
        )
    report.notes.append(
        "Restart Codex, then check `codex mcp list` shows 'fusion' and ask it to call fusion_stats."
    )
    report.notes.append(
        f"Codex forwards only the variables listed in env_vars ({', '.join(PROVIDER_KEYS)}) to "
        "the server: export your provider keys in the shell you start Codex from."
    )
    report.notes.append(
        "Codex does not use MCP progress notifications, resources or prompts, so a call shows "
        "no progress until it returns; Fusion's tools work the same without them."
    )
    return report


# -- the TOML file ---------------------------------------------------------------------------


def load_toml(path: Path) -> TOMLDocument:
    """A TOML file as an editable document; a missing file is empty, a malformed one is refused."""
    if not path.exists():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    except TOMLKitError as exc:
        msg = f"{path} is not valid TOML ({exc}); fix or remove it, then run the install again."
        raise InstallError(msg) from exc


def _number(value: object) -> TypeGuard[float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def apply_server(document: TOMLDocument, spec: ServerSpec, *, force: bool) -> list[str]:
    """Make ``[mcp_servers.fusion]`` right, editing ``document`` in place.

    Returns what changed (empty when nothing had to). A different command under the same name is
    refused unless ``force``; timeouts that are already high enough are kept.
    """
    servers = document.get("mcp_servers")
    if servers is None:
        servers = tomlkit.table(is_super_table=True)
        document["mcp_servers"] = servers
    if not isinstance(servers, dict):
        msg = (
            "'mcp_servers' in the Codex config is not a table; fix it, then run the install again."
        )
        raise InstallError(msg)
    entry = servers.get(SERVER_NAME)
    changes: list[str] = []
    if entry is None:
        entry = tomlkit.table()
        entry["command"] = spec.command
        entry["args"] = _array(spec.args)
        servers[SERVER_NAME] = entry
        changes.append(f"added [mcp_servers.{SERVER_NAME}]")
    elif not isinstance(entry, dict):
        msg = f"[mcp_servers.{SERVER_NAME}] is not a table; fix it, then run the install again."
        raise InstallError(msg)
    elif not spec.matches(entry.unwrap() if hasattr(entry, "unwrap") else entry):
        if not force:
            msg = (
                f"The Codex config already has a different '{SERVER_NAME}' server "
                f"({_describe(entry)}). Run again with --force to replace its command."
            )
            raise InstallError(msg)
        entry["command"] = spec.command
        entry["args"] = _array(spec.args)
        changes.append("replaced its command")

    for key, wanted in (
        ("startup_timeout_sec", STARTUP_TIMEOUT_S),
        ("tool_timeout_sec", TOOL_TIMEOUT_S),
    ):
        have = entry.get(key)
        if not _number(have) or have < wanted:
            entry[key] = wanted
            changes.append(f"{key} = {wanted}" + (f" (was {have})" if have is not None else ""))

    forwarded = entry.get("env_vars")
    if forwarded is None:
        entry["env_vars"] = _array(PROVIDER_KEYS, multiline=False)
        changes.append("env_vars forwards the provider keys")
    elif isinstance(forwarded, list):
        missing = [key for key in PROVIDER_KEYS if key not in forwarded]
        if missing:
            forwarded.extend(missing)
            changes.append(f"env_vars adds {', '.join(missing)}")
    else:
        msg = f"[mcp_servers.{SERVER_NAME}] env_vars is not a list; fix it, then run again."
        raise InstallError(msg)
    return changes


def _array(items: tuple[str, ...], *, multiline: bool = True) -> Any:
    array = tomlkit.array()
    array.extend(items)
    if multiline:
        array.multiline(True)
    return array


def _describe(entry: Any) -> str:
    return " ".join([str(entry.get("command", "?")), *map(str, entry.get("args", []))])


# -- the AGENTS.md block ----------------------------------------------------------------------


def agents_block() -> str:
    return f"{BEGIN}\n{integration_text('codex', 'AGENTS.fusion.md').rstrip()}\n{END}\n"


def with_agents_block(current: str) -> tuple[str, str]:
    """``current`` with Fusion's block added or refreshed, and ``same``, ``added`` or ``replaced``.

    Only the text between the markers is ever replaced. A lone marker means the block was damaged
    by hand and is refused, since guessing where it ends could delete the user's text.
    """
    block = agents_block()
    has_begin, has_end = BEGIN in current, END in current
    if has_begin != has_end or (has_begin and not _BLOCK.search(current)):
        msg = (
            "AGENTS.md has a damaged Fusion block (one marker without the other, or out of "
            f"order). Remove the lines between {BEGIN.split(' (')[0]} and {END}, then run again."
        )
        raise InstallError(msg)
    if has_begin:
        updated = _BLOCK.sub(lambda _m: block, current, count=1)
        return updated, ("same" if updated == current else "replaced")
    if not current:
        return block, "added"
    return current + ("\n" if current.endswith("\n") else "\n\n") + block, "added"


# -- what `fusion doctor` reads ---------------------------------------------------------------


def inspect_codex(
    *,
    project_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[ClientCheck]:
    """Read-only findings about Codex's Fusion setup, for the project and for the user."""
    codex_dir = codex_home(environ, home)
    return [
        _inspect_file(
            config_path(project=project, project_dir=project_dir, codex_dir=codex_dir),
            "project" if project else "user",
        )
        for project in (True, False)
    ]


def _inspect_file(path: Path, scope: str) -> ClientCheck:
    client = "codex"
    fix = "fusion install codex" + (" --project" if scope == "project" else "")
    if not path.exists():
        return ClientCheck(client, str(path), "absent", f"no {scope} config file", fix)
    try:
        document = load_toml(path)
    except InstallError as exc:
        return ClientCheck(client, str(path), "error", str(exc), f"fix or remove {path}")
    servers = document.get("mcp_servers")
    entry = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        return ClientCheck(client, str(path), "absent", f"no '{SERVER_NAME}' server in it", fix)
    data = entry.unwrap() if hasattr(entry, "unwrap") else dict(entry)
    if not is_fusion_entry(data.get("command"), data.get("args")):
        return ClientCheck(
            client,
            str(path),
            "warn",
            f"'{SERVER_NAME}' does not run `fusion mcp`",
            f"{fix} --force",
        )
    problems: list[str] = []
    if data.get("enabled") is False:
        problems.append("it is disabled (enabled = false)")
    timeout = data.get("tool_timeout_sec")
    if not _number(timeout) or timeout < TOOL_TIMEOUT_S:
        problems.append(
            f"tool_timeout_sec is {timeout if _number(timeout) else 'unset (Codex default 60)'}, "
            "so Codex can abort a call before Fusion's own 90 s limit"
        )
    startup = data.get("startup_timeout_sec")
    if not _number(startup) or startup < STARTUP_TIMEOUT_S:
        problems.append("startup_timeout_sec is too short for the first start (it builds Fusion)")
    keys = set(data.get("env_vars", [])) | set(data.get("env", {}))
    if not keys & set(PROVIDER_KEYS):
        problems.append("no provider key is forwarded (env_vars)")
    if problems:
        return ClientCheck(client, str(path), "warn", "; ".join(problems), fix)
    return ClientCheck(client, str(path), "ok", f"'{SERVER_NAME}' is registered")
