"""How a headless Claude Code run is started, and how its output is read.

``build_argv`` makes the ``claude -p`` command line: stream-json output (the final ``result`` event
carries Claude Code's own cost and usage; the assistant events before it show every tool call),
``--bare`` so the owner's hooks, CLAUDE.md files, plugins and keychain never take part, a per-run
``--max-budget-usd``, MCP servers only from ``--mcp-config``, and permission prompts answered "no".

``claude_settings`` is the ``--settings`` file: Claude Code's own sandbox runs every Bash command
with no network (an empty, strict allow-list) and refuses to fall back to running unsandboxed;
WebFetch and WebSearch are denied; the directory holding the run's MCP config (which carries the
provider keys the Fusion server needs) cannot be read. File edits are limited to the working
directory by ``acceptEdits`` plus ``--permission-prompts none``: an edit anywhere else would need a
prompt, and nobody answers it.

``parse_output`` reads either output format (one JSON object, or JSON lines) and never raises: a
run that printed nothing usable is a ``ClaudeResult`` with ``error`` set.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "FUSION_SERVER",
    "ClaudeInvocation",
    "ClaudeResult",
    "build_argv",
    "claude_settings",
    "is_fusion_tool",
    "mcp_config",
    "parse_output",
]

FUSION_SERVER = "fusion"  # the MCP server's name in --mcp-config: its tools are mcp__fusion__*
# Tool-name prefixes of Fusion's MCP tools: from --mcp-config, and from the plugin's own .mcp.json.
_FUSION_PREFIXES = (f"mcp__{FUSION_SERVER}__", "mcp__plugin_fusion_fusion__")


def is_fusion_tool(name: str) -> bool:
    return name.startswith(_FUSION_PREFIXES)


def claude_settings(*, secrets_dir: Path) -> dict[str, Any]:
    """The ``--settings`` JSON for one run (see the module docstring)."""
    hidden = str(secrets_dir)
    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "network": {"allowedDomains": [], "strictAllowlist": True},
            "filesystem": {"denyRead": [hidden]},
        },
        "permissions": {
            "deny": [
                "WebFetch",
                "WebSearch",
                f"Read(/{hidden}/**)",  # ``//`` starts an absolute path in a permission rule
                f"Edit(/{hidden}/**)",
            ],
        },
    }


def mcp_config(server_cmd: Sequence[str] | None, env: Mapping[str, str]) -> dict[str, Any]:
    """The ``--mcp-config`` JSON: the Fusion server, or no server at all (``server_cmd`` None)."""
    if server_cmd is None:
        return {"mcpServers": {}}
    return {
        "mcpServers": {
            FUSION_SERVER: {
                "command": server_cmd[0],
                "args": list(server_cmd[1:]),
                "env": dict(env),
            }
        }
    }


@dataclass(frozen=True)
class ClaudeInvocation:
    """What one run is started with, besides the executable."""

    model: str  # a full model id, so the arm is pinned (an alias would follow new releases)
    prompt: str
    max_budget_usd: float  # Claude Code stops the session when its own cost reaches this
    settings_path: Path
    mcp_config_path: Path
    plugin_dir: Path | None = None
    append_system_prompt: str = ""
    allowed_tools: list[str] = field(default_factory=list)


def build_argv(claude: Sequence[str], inv: ClaudeInvocation) -> list[str]:
    argv = [
        *claude,
        "-p",
        inv.prompt,
        "--output-format",
        "stream-json",
        "--verbose",  # stream-json in print mode needs it
        "--model",
        inv.model,
        "--max-budget-usd",
        f"{inv.max_budget_usd:g}",
        "--bare",
        "--no-session-persistence",
        "--permission-mode",
        "acceptEdits",
        "--permission-prompts",
        "none",
        "--settings",
        str(inv.settings_path),
        "--strict-mcp-config",
        "--mcp-config",
        str(inv.mcp_config_path),
    ]
    if inv.plugin_dir is not None:
        argv += ["--plugin-dir", str(inv.plugin_dir)]
    if inv.allowed_tools:
        argv += ["--allowedTools", ",".join(inv.allowed_tools)]
    if inv.append_system_prompt:
        argv += ["--append-system-prompt", inv.append_system_prompt]
    return argv


class ClaudeResult(BaseModel):
    """What a run printed, reduced to what the study measures."""

    ok: bool
    error: str | None = None
    result_text: str = ""
    num_turns: int = 0
    duration_ms: float | None = None
    host_cost_usd: float | None = None  # Claude Code's own figure; None: it printed none
    input_tokens: int = 0  # fresh input, cache writes and cache reads together
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    model_usage: dict[str, Any] = Field(default_factory=dict)
    tool_calls: dict[str, int] = Field(default_factory=dict)  # tool name -> calls
    fusion_connected: bool | None = None  # None: the output had no init event to say
    permission_denials: int = 0
    session_id: str | None = None

    @property
    def total_tool_calls(self) -> int:
        return sum(self.tool_calls.values())

    @property
    def fusion_tool_calls(self) -> int:
        return sum(n for name, n in self.tool_calls.items() if is_fusion_tool(name))


def _events(stdout: str) -> list[dict[str, Any]]:
    text = stdout.strip()
    if not text:
        return []
    try:
        whole = json.loads(text)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict):
        return [whole]
    if isinstance(whole, list):
        return [e for e in whole if isinstance(e, dict)]
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # a stray log line
        if isinstance(event, dict):
            events.append(event)
    return events


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _cost(result: Mapping[str, Any]) -> float | None:
    total = result.get("total_cost_usd")
    if isinstance(total, int | float):
        return float(total)
    usage = result.get("modelUsage")
    if isinstance(usage, dict):
        costs = [
            float(m["costUSD"])
            for m in usage.values()
            if isinstance(m, dict) and isinstance(m.get("costUSD"), int | float)
        ]
        if costs:
            return sum(costs)
    return None


def parse_output(stdout: str) -> ClaudeResult:
    """The run's result from ``--output-format json`` or ``stream-json`` output."""
    events = _events(stdout)
    tools: Counter[str] = Counter()
    connected: bool | None = None
    result: dict[str, Any] | None = None
    for event in events:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            servers = event.get("mcp_servers") or []
            connected = any(
                isinstance(s, dict)
                and s.get("name") == FUSION_SERVER
                and s.get("status") == "connected"
                for s in servers
            )
        elif kind == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tools[str(block.get("name", "?"))] += 1
        elif kind == "result":
            result = event
    if result is None:
        return ClaudeResult(
            ok=False,
            error="Claude Code printed no result event" if events else "no result: no JSON output",
            tool_calls=dict(tools),
            fusion_connected=connected,
        )
    usage = result.get("usage") or {}
    fresh = _int(usage.get("input_tokens"))
    reads = _int(usage.get("cache_read_input_tokens"))
    writes = _int(usage.get("cache_creation_input_tokens"))
    failed = bool(result.get("is_error")) or result.get("subtype") not in (None, "success")
    duration = result.get("duration_ms")
    return ClaudeResult(
        ok=not failed,
        error=str(result.get("subtype") or "error") if failed else None,
        result_text=str(result.get("result") or ""),
        num_turns=_int(result.get("num_turns")),
        duration_ms=float(duration) if isinstance(duration, int | float) else None,
        host_cost_usd=_cost(result),
        input_tokens=fresh + reads + writes,
        output_tokens=_int(usage.get("output_tokens")),
        cache_read_tokens=reads,
        cache_creation_tokens=writes,
        model_usage=dict(result.get("modelUsage") or {}),
        tool_calls=dict(tools),
        fusion_connected=connected,
        permission_denials=len(result.get("permission_denials") or []),
        session_id=result.get("session_id"),
    )
