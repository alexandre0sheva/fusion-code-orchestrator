"""Workspace tool executor for the coding agent."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from fusion.agent.workspace import WorkspaceGuard
from fusion.security.redaction import redact_secrets


class WorkspaceToolExecutor:
    """Executes read/write/list/run tools within a guarded workspace."""

    def __init__(
        self,
        *,
        guard: WorkspaceGuard,
        allow_shell: bool,
        command_timeout: int = 120,
    ) -> None:
        self._guard = guard
        self._allow_shell = allow_shell
        self._command_timeout = command_timeout
        self.files_changed: set[str] = set()

    def execute(self, action: dict[str, Any]) -> str:
        """Run one agent action and return observation text for the model."""
        name = str(action.get("action", "")).strip().lower()
        if name == "read_file":
            return self._read_file(str(action.get("path", "")))
        if name == "write_file":
            return self._write_file(str(action.get("path", "")), str(action.get("content", "")))
        if name == "list_dir":
            return self._list_dir(str(action.get("path", ".")))
        if name == "run_command":
            return self._run_command(
                str(action.get("command", "")),
                str(action.get("cwd", ".")),
            )
        if name == "done":
            return str(action.get("summary", "Task marked done."))
        return f"Unknown action: {name}. Use read_file, write_file, list_dir, run_command, or done."

    def _read_file(self, path: str) -> str:
        target = self._guard.resolve(path)
        if not target.is_file():
            return f"Error: file not found: {path}"
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > 12000:
            return text[:12000] + "\n...[truncated]..."
        return text

    def _write_file(self, path: str, content: str) -> str:
        target = self._guard.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        rel = self._guard.relative(target)
        self.files_changed.add(rel)
        return f"Wrote {rel} ({len(content)} bytes)"

    def _list_dir(self, path: str) -> str:
        target = self._guard.resolve(path)
        if not target.is_dir():
            return f"Error: directory not found: {path}"
        entries = sorted(target.iterdir(), key=lambda p: p.name)
        lines = [self._guard.relative(p) + ("/" if p.is_dir() else "") for p in entries[:200]]
        return "\n".join(lines) if lines else "(empty directory)"

    def _run_command(self, command: str, cwd: str) -> str:
        if not self._allow_shell:
            return "Error: shell execution disabled (set FUSION_AGENT_MODE=true)"
        sanitized = redact_secrets(command)
        if sanitized.redaction_count:
            return "Error: command rejected — possible secret in command string"
        workdir = self._guard.resolve(cwd)
        if not workdir.is_dir():
            return f"Error: cwd not found: {cwd}"
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=self._command_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {self._command_timeout}s"
        output = (proc.stdout or "") + (proc.stderr or "")
        if len(output) > 8000:
            output = output[:8000] + "\n...[truncated]..."
        return f"exit_code={proc.returncode}\n{output}".strip()


async def run_verify_command(
    *,
    guard: WorkspaceGuard,
    command: str,
    timeout: int = 300,
) -> tuple[int, str]:
    """Run a verification command in the workspace root."""
    if not command.strip():
        return 0, ""
    sanitized = redact_secrets(command)
    if sanitized.redaction_count:
        return 1, "Verification command rejected — possible secret in command"

    def _run() -> tuple[int, str]:
        proc = subprocess.run(
            sanitized.text,
            shell=True,
            cwd=guard.root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, output[:8000]

    return await asyncio.to_thread(_run)
