"""Secret redaction before sending content to external providers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Patterns ordered from most specific to general
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_key", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    ("github_token", re.compile(r"\b(ghp_[a-zA-Z0-9]{36,})\b")),
    ("github_token", re.compile(r"\b(github_pat_[a-zA-Z0-9_]{20,})\b")),
    ("jwt", re.compile(r"\b(eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)\b")),
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END"
        ),
    ),
    (
        "api_key_assignment",
        re.compile(
            r"(?i)((?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token"
            r"|password|passwd|pwd)\s*[=:]\s*)['\"]?([^\s'\"]{8,})['\"]?"
        ),
    ),
    (
        "env_line",
        re.compile(
            r"(?m)^([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD)[A-Z0-9_]*)=(.+)$"
        ),
    ),
    ("bearer_token", re.compile(r"(?i)(Bearer\s+)([a-zA-Z0-9\-._~+/]+=*)")),
    ("sk_key", re.compile(r"\b(sk-[a-zA-Z0-9]{20,})\b")),
    ("anthropic_key", re.compile(r"\b(sk-ant-[a-zA-Z0-9\-_]{20,})\b")),
]

_REDACTED = "[REDACTED]"


@dataclass
class RedactionResult:
    """Result of redacting secrets from text."""

    text: str
    redaction_count: int = 0
    redacted_types: list[str] = field(default_factory=list)


def redact_secrets(text: str) -> RedactionResult:
    """Redact known secret patterns from text.

    Returns sanitized text and metadata about what was redacted.
    """
    result = text
    count = 0
    types: list[str] = []

    for name, pattern in _PATTERNS:
        def _replace_api_key(m: re.Match[str]) -> str:
            return f"{m.group(1)}{_REDACTED}"

        def _replace_env(m: re.Match[str]) -> str:
            return f"{m.group(1)}={_REDACTED}"

        def _replace_bearer(m: re.Match[str]) -> str:
            return f"{m.group(1)}{_REDACTED}"

        if name == "api_key_assignment":
            new_result, n = pattern.subn(_replace_api_key, result)
        elif name == "env_line":
            new_result, n = pattern.subn(_replace_env, result)
        elif name == "bearer_token":
            new_result, n = pattern.subn(_replace_bearer, result)
        else:
            new_result, n = pattern.subn(_REDACTED, result)

        if n > 0:
            count += n
            types.append(name)
        result = new_result

    return RedactionResult(text=result, redaction_count=count, redacted_types=types)
