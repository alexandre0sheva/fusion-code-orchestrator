"""Security policy for logging of prompts."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class SecurityPolicy:
    """Controls what data may be logged."""

    log_raw_prompts: bool = False

    @classmethod
    def from_env(cls) -> SecurityPolicy:
        raw = os.environ.get("FUSION_LOG_RAW_PROMPTS", "false").lower()
        return cls(log_raw_prompts=raw in ("1", "true", "yes"))

    def sanitize_for_log(self, text: str, redacted_text: str) -> str:
        """Return appropriate text for logging based on policy."""
        if self.log_raw_prompts:
            return text
        return redacted_text
