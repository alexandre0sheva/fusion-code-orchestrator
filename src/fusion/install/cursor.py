"""``fusion install cursor``: register the Fusion MCP server (and a rule) with Cursor.

Cursor reads MCP servers from ``.cursor/mcp.json`` in a project or ``~/.cursor/mcp.json`` for every
project, both in the ``mcpServers`` shape Claude Code's ``.mcp.json`` uses, so the merge is the
shared one: other servers and keys stay, a different ``fusion`` entry is refused without
``--force``, and a malformed file is refused untouched.

The rule (``.cursor/rules/fusion.mdc``) tells the agent when to call Fusion. Cursor has no global
rules *file* (user rules live in its settings), so it is written for a project install only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fusion.install.common import (
    SERVER_NAME,
    ClientCheck,
    InstallError,
    InstallReport,
    ServerSpec,
    checkout_spec,
    integration_text,
    is_fusion_entry,
    merge_server,
    read_json_object,
    uvx_spec,
    verify_server,
    write_text_atomic,
)

Verifier = Callable[[ServerSpec], list[str]]
RULE_PATH = Path(".cursor") / "rules" / "fusion.mdc"


@dataclass
class CursorOptions:
    global_: bool = False  # ~/.cursor/mcp.json instead of the project's .cursor/mcp.json
    rules: bool = True  # also write .cursor/rules/fusion.mdc (project install only)
    ref: str | None = None  # pin the uvx source to a tag, branch or commit
    checkout: Path | None = None  # run from a local clone instead of GitHub
    dry_run: bool = False
    force: bool = False  # replace a different 'fusion' server or an edited rule
    verify: bool = True
    project_dir: Path | None = None  # the project (default: cwd)


def config_path(
    *, global_: bool, project_dir: Path | None = None, home: Path | None = None
) -> Path:
    base = (home or Path.home()) if global_ else (project_dir or Path.cwd())
    return base / ".cursor" / "mcp.json"


def install_cursor(
    options: CursorOptions,
    *,
    verifier: Verifier = verify_server,
    home: Path | None = None,
) -> InstallReport:
    """Install, or with ``dry_run`` only report what would happen. Raises ``InstallError``."""
    report = InstallReport(dry_run=options.dry_run)
    spec = checkout_spec(options.checkout) if options.checkout else uvx_spec(options.ref)
    path = config_path(global_=options.global_, project_dir=options.project_dir, home=home)

    # Everything that can refuse is read before anything is written, so a refusal changes nothing.
    outcome = merge_server(path, spec, force=options.force, dry_run=True)
    rule_path = _rule_target(options)
    rule_state = _rule_state(rule_path, options) if rule_path else None

    if options.verify and not options.dry_run:
        names = verifier(spec)
        report.checks.append(f"The server starts and lists {len(names)} tools")
    elif options.verify:
        report.commands.append(f"(check) start `{spec.shell()}` and list its tools")

    if outcome == "unchanged":
        report.unchanged.append(f"{path} already registers '{SERVER_NAME}'")
    else:
        merge_server(path, spec, force=options.force, dry_run=options.dry_run)
        verb = "Would write" if options.dry_run else "Wrote"
        report.actions.append(f"{verb} the '{SERVER_NAME}' server to {path} ({outcome})")

    if rule_path and rule_state == "same":
        report.unchanged.append(f"{rule_path} is already the Fusion rule")
    elif rule_path and rule_state:
        if not options.dry_run:
            write_text_atomic(rule_path, integration_text("cursor", "rules", "fusion.mdc"))
        verb = "Would write" if options.dry_run else "Wrote"
        report.actions.append(f"{verb} the Fusion rule to {rule_path} ({rule_state})")
    elif options.global_ and options.rules:
        report.notes.append(
            "Cursor has no global rules file, so no rule was written. To teach the agent when to "
            "call Fusion everywhere, paste the body of integrations/cursor/rules/fusion.mdc into "
            "Cursor Settings > Rules."
        )

    report.notes.append(
        "Restart Cursor (or toggle 'fusion' in Settings > Tools & MCP), then ask the agent to "
        "call fusion_stats."
    )
    report.notes.append(
        "Provider keys come from the environment Cursor starts in (launch it from a shell that "
        "has ANTHROPIC_API_KEY, OPENAI_API_KEY or GOOGLE_API_KEY exported); a project .env is "
        "also read when the server starts in the project directory."
    )
    return report


def _rule_target(options: CursorOptions) -> Path | None:
    if options.global_ or not options.rules:
        return None
    return (options.project_dir or Path.cwd()) / RULE_PATH


def _rule_state(path: Path, options: CursorOptions) -> str:
    """``same``, ``added`` or ``replaced``; an edited rule is refused without ``--force``."""
    wanted = integration_text("cursor", "rules", "fusion.mdc")
    if not path.exists():
        return "added"
    if path.read_text(encoding="utf-8") == wanted:
        return "same"
    if not options.force:
        msg = (
            f"{path} exists and differs from Fusion's rule (you may have edited it). Run again "
            "with --force to replace it, or use --no-rules to leave it alone."
        )
        raise InstallError(msg)
    return "replaced"


# -- what `fusion doctor` reads ---------------------------------------------------------------


def inspect_cursor(
    *, project_dir: Path | None = None, home: Path | None = None
) -> list[ClientCheck]:
    """Read-only findings about Cursor's Fusion setup, for the project and for the user."""
    findings: list[ClientCheck] = []
    for global_ in (False, True):
        path = config_path(global_=global_, project_dir=project_dir, home=home)
        findings.append(_inspect_file(path, "user" if global_ else "project"))
    return findings


def _inspect_file(path: Path, scope: str) -> ClientCheck:
    client = "cursor"
    fix = f"fusion install cursor{' --global' if scope == 'user' else ''}"
    if not path.exists():
        return ClientCheck(client, str(path), "absent", f"no {scope} config file", fix)
    try:
        servers = read_json_object(path).get("mcpServers", {})
    except InstallError as exc:
        return ClientCheck(client, str(path), "error", str(exc), f"fix or remove {path}")
    entry = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    if entry is None:
        return ClientCheck(client, str(path), "absent", f"no '{SERVER_NAME}' server in it", fix)
    if not isinstance(entry, dict) or not is_fusion_entry(entry.get("command"), entry.get("args")):
        return ClientCheck(
            client,
            str(path),
            "warn",
            f"'{SERVER_NAME}' is registered but does not run `fusion mcp`",
            f"{fix} --force",
        )
    return ClientCheck(client, str(path), "ok", f"'{SERVER_NAME}' is registered")
