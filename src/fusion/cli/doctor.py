"""``fusion doctor``: is this machine ready to run Fusion, and where is it not?

Each check returns a status (``ok``, ``warn``, ``error``, ``info`` or ``skip``), a detail and, when
something is wrong, the command or edit that fixes it. Nothing is changed and nothing costs money:
``--live`` asks each provider for its model list (a free endpoint that also proves the key works),
not for a completion. Exit code 1 when any check is an ``error`` (``--strict``: or a ``warn``).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Literal

import httpx
import typer
from rich.markup import escape

from fusion import __version__
from fusion.cli.common import PROVIDER_KEYS, JsonOption, console, echo_json
from fusion.config import paths as fusion_paths
from fusion.config.catalog import catalog_warnings, load_catalog
from fusion.config.catalog_check import ModelCheck, check_catalog_live
from fusion.config.layers import ConfigError, resolve_config
from fusion.install.checks import inspect_clients
from fusion.install.common import InstallError, ServerSpec, verify_server

Status = Literal["ok", "warn", "error", "info", "skip"]
MIN_PYTHON = (3, 12)


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""


McpCheck = Callable[[], list[str]]
LiveCheck = Callable[[Mapping[str, str]], list[ModelCheck]]


def mask(secret: str) -> str:
    """Enough of a key to recognise it, never enough to use it."""
    return f"…{secret[-4:]}" if len(secret) >= 12 else "set"


def default_mcp_check() -> list[str]:
    """Start ``fusion mcp`` as a client would, on the mock provider and a scratch database.

    A server whose stdout carries anything but JSON-RPC (a stray print) fails the handshake here.
    """
    spec = ServerSpec(sys.executable, ("-m", "fusion.main", "mcp"))
    with tempfile.TemporaryDirectory() as scratch:
        return verify_server(
            spec, timeout=60.0, extra_env={"FUSION_DB_PATH": str(Path(scratch) / "doctor.db")}
        )


def default_live_check(keys: Mapping[str, str]) -> list[ModelCheck]:
    async def _run() -> list[ModelCheck]:
        async with httpx.AsyncClient() as client:
            return await check_catalog_live(load_catalog(), dict(keys), client)

    return asyncio.run(_run())


def run_checks(
    *,
    live: bool = False,
    mcp: bool = True,
    environ: Mapping[str, str] | None = None,
    project_dir: Path | None = None,
    home: Path | None = None,
    mcp_check: McpCheck | None = None,
    live_check: LiveCheck | None = None,
) -> list[Check]:
    """Every check, in the order a person would fix them."""
    env = os.environ if environ is None else environ
    checks = [*_runtime(), *_config()]
    config_ok = checks[-1].status != "error"
    if not config_ok:
        # Everything below reads the configuration; say so once instead of failing in each.
        checks.append(Check("keys, catalog", "skip", "needs a valid configuration (see config)"))
    else:
        checks.extend(_safe("keys", lambda: _keys(env)))
        checks.extend(_safe("catalog", _catalog))
    checks.extend(_safe("database", lambda: [_database()]))
    if mcp:
        checks.append(_mcp(mcp_check or default_mcp_check))
    checks.extend(_safe("clients", lambda: _clients(env, project_dir, home)))
    if live and config_ok:
        checks.extend(_safe("live", lambda: _live(env, live_check or default_live_check)))
    return checks


def _safe(name: str, check: Callable[[], list[Check]]) -> list[Check]:
    """One check that fails unexpectedly is a finding, not the end of the report."""
    try:
        return check()
    except Exception as exc:
        return [Check(name, "error", f"the check itself failed: {exc}", "run with --verbose")]


def _runtime() -> list[Check]:
    version = ".".join(str(part) for part in sys.version_info[:3])
    python = (
        Check("python", "ok", f"Python {version}")
        if sys.version_info >= MIN_PYTHON
        else Check(
            "python",
            "error",
            f"Python {version}; Fusion needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer",
            "uv python install 3.12, then run Fusion with `uvx --python '>=3.12' ...`",
        )
    )
    uv = shutil.which("uv")
    uv_check = (
        Check("uv", "ok", uv)
        if uv
        else Check(
            "uv",
            "warn",
            "`uv` is not on PATH; the one-command client installs (uvx) need it",
            "install it from https://docs.astral.sh/uv/",
        )
    )
    return [Check("fusion", "ok", f"fusion-code-orchestrator {__version__}"), python, uv_check]


def _config() -> list[Check]:
    try:
        config = resolve_config()
    except ConfigError as exc:
        return [
            Check("config", "error", str(exc), "fix the file named above; `fusion config paths`")
        ]
    loaded = [layer.name for layer in config.layers if layer.path is not None]
    detail = "packaged defaults" + "".join(f" + {name}" for name in loaded)
    return [Check("config", "ok", detail)]


def _keys(env: Mapping[str, str]) -> list[Check]:
    models = load_catalog().models.values()
    providers = [
        provider
        for provider in PROVIDER_KEYS
        if any(m.enabled and m.provider == provider for m in models)
    ]
    present = {p: env.get(PROVIDER_KEYS[p], "").strip() for p in providers}
    none_set = not any(present.values())
    checks: list[Check] = []
    for provider in providers:
        variable = PROVIDER_KEYS[provider]
        if present[provider]:
            checks.append(
                Check(f"key:{provider}", "ok", f"{variable} is set ({mask(present[provider])})")
            )
        else:
            checks.append(
                Check(
                    f"key:{provider}",
                    "error" if none_set else "warn",
                    f"{variable} is not set" + ("" if none_set else "; its models are skipped"),
                    f"export {variable}=... or put it in a .env file in the directory you run from",
                )
            )
    return checks


def _catalog() -> list[Check]:
    warnings = catalog_warnings(load_catalog())
    if not warnings:
        return [Check("catalog", "ok", "prices and model ids are current")]
    return [
        Check("catalog", "warn", warning, "fusion models check --live; update config/models.yaml")
        for warning in warnings
    ]


def _database() -> Check:
    path = fusion_paths.resolve_db_path()
    folder = path.parent
    fix = f"make {folder} writable, or set FUSION_DB_PATH to a file you can write"
    probe = folder
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not os.access(probe, os.W_OK):
        return Check("database", "error", f"{folder} is not writable", fix)
    if path.exists() and not os.access(path, os.W_OK):
        return Check("database", "error", f"{path} is not writable", fix)
    state = "exists" if path.exists() else "will be created on first run"
    return Check("database", "ok", f"{path} ({state})")


def _mcp(check: McpCheck) -> Check:
    try:
        names = check()
    except InstallError as exc:
        return Check(
            "mcp-server",
            "error",
            str(exc).splitlines()[0],
            "run `fusion mcp` under a client, never print to stdout in code the server reaches",
        )
    return Check("mcp-server", "ok", f"starts, stdout is clean JSON-RPC, lists {len(names)} tools")


def _clients(env: Mapping[str, str], project_dir: Path | None, home: Path | None) -> list[Check]:
    by_client: dict[str, list[Check]] = {}
    for finding in inspect_clients(project_dir=project_dir, environ=env, home=home):
        status: Status = "info" if finding.status == "absent" else finding.status
        check = Check(
            f"client:{finding.client}",
            status,
            f"{finding.location}: {finding.message}",
            finding.fix,
        )
        by_client.setdefault(finding.client, []).append(check)
    checks: list[Check] = []
    for client, found in by_client.items():
        real = [c for c in found if c.status != "info"]
        if real:
            checks.extend(real)
        else:
            checks.append(Check(f"client:{client}", "info", "not configured", found[0].fix))
    return checks


def _live(env: Mapping[str, str], check: LiveCheck) -> list[Check]:
    keys = {p: env[v] for p, v in PROVIDER_KEYS.items() if env.get(v, "").strip()}
    if not keys:
        return [Check("live", "skip", "no provider key is set, so nothing to ask")]
    labels: dict[str, Status] = {
        "ok": "ok",
        "unknown_id": "error",
        "skipped": "skip",
        "error": "error",
    }
    out: list[Check] = []
    for item in check(keys):
        out.append(
            Check(
                f"live:{item.alias}",
                labels[item.status],
                f"{item.provider}/{item.model_id}" + (f" ({item.detail})" if item.detail else ""),
                "fusion models check --live" if item.status in {"unknown_id", "error"} else "",
            )
        )
    return out


# -- the command ---------------------------------------------------------------------------------

_STYLE = {"ok": "green", "warn": "yellow", "error": "red", "info": "dim", "skip": "dim"}
_LABEL = {"ok": "ok", "warn": "WARN", "error": "FAIL", "info": "info", "skip": "skip"}


def doctor(
    live: Annotated[
        bool,
        typer.Option(
            "--live", help="Also ask each provider for its model list (free; proves keys)"
        ),
    ] = False,
    mcp: Annotated[
        bool, typer.Option("--mcp/--no-mcp", help="Start the MCP server and list its tools")
    ] = True,
    strict: Annotated[bool, typer.Option(help="Exit 1 on warnings too")] = False,
    as_json: JsonOption = False,
) -> None:
    """Check Python, keys, config, catalog, database, the MCP server and client setups."""
    checks = run_checks(live=live, mcp=mcp)
    failed = any(c.status == "error" for c in checks) or (
        strict and any(c.status == "warn" for c in checks)
    )
    if as_json:
        echo_json({"ok": not failed, "checks": [asdict(c) for c in checks]})
    else:
        for check in checks:
            style = _STYLE[check.status]
            console.print(
                f"[{style}]{_LABEL[check.status]:<5}[/{style}] {escape(check.name):<22} "
                f"{escape(check.detail)}",
                soft_wrap=True,
                highlight=False,
                markup=True,
            )
            if check.fix and check.status in {"warn", "error"}:
                console.print(
                    f"      fix: {check.fix}", soft_wrap=True, highlight=False, style="dim"
                )
        problems = sum(c.status == "error" for c in checks)
        notes = sum(c.status == "warn" for c in checks)
        console.print(
            f"\n{problems} problem(s), {notes} warning(s)."
            if problems or notes
            else "\nEverything checks out."
        )
    if failed:
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    app.command("doctor")(doctor)
