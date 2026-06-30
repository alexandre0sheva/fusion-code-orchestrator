"""Security policy for logging and external calls."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class SecurityPolicy:
    """Controls what data may be logged or sent externally."""

    log_raw_prompts: bool = False
    allow_file_writes: bool = False
    allow_shell_execution: bool = False
    workspace_root: str = ""

    @classmethod
    def from_env(cls) -> SecurityPolicy:
        raw = os.environ.get("FUSION_LOG_RAW_PROMPTS", "false").lower()
        agent = os.environ.get("FUSION_AGENT_MODE", "false").lower() in ("1", "true", "yes", "on")
        workspace = os.environ.get("FUSION_WORKSPACE_ROOT", os.getcwd())
        return cls(
            log_raw_prompts=raw in ("1", "true", "yes"),
            allow_file_writes=agent,
            allow_shell_execution=agent,
            workspace_root=workspace,
        )

    def sanitize_for_log(self, text: str, redacted_text: str) -> str:
        """Return appropriate text for logging based on policy."""
        if self.log_raw_prompts:
            return text
        return redacted_text
