"""Secret redaction before content reaches an external provider or the run store.

Two layers: rules for credentials with a recognisable shape (provider keys, tokens, key blocks,
``.env`` lines, connection strings) and an optional entropy rule for long random-looking tokens
that have no known prefix. Both only ever replace the secret itself, so the surrounding code
stays readable for the model.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import cache, lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fusion.providers.base import ModelRequest

_REDACTED = "[REDACTED]"

# A rule replaces its ``secret`` group, or the whole match when it has none. Order matters: the
# most specific shapes go first so the reported type names the kind of secret, and a later, looser
# rule finds only what is left (it skips values that are already ``[REDACTED]``).
_PEM_BODY = r"[A-Za-z0-9+/=:,.\-_ \t\r\n]"
_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "private_key",
        re.compile(
            rf"-----BEGIN (?P<kind>[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?)-----{_PEM_BODY}{{0,20000}}?"
            r"-----END (?P=kind)-----"
        ),
    ),
    (  # a key whose footer was cut off (a truncated paste or diff hunk): its body still goes
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
            r"(?:[ \t]*\r?\n[A-Za-z0-9+/=]{16,}[ \t]*)+"
        ),
    ),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("sk_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])")),
    ("google_oauth", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,})"),
    ),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{20,}")),
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("jwt", re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b")),
    (  # the password of ``scheme://user:password@host``
        "url_credentials",
        re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@'\"]+:(?P<secret>[^\s/@'\"]+)@"),
    ),
    (
        "aws_secret",
        re.compile(
            r"(?i)aws[_-]?secret[_-]?(?:access[_-]?)?key\s*[=:]\s*['\"]?"
            r"(?P<secret>[A-Za-z0-9/+=]{30,})"
        ),
    ),
    (
        "api_key_assignment",
        re.compile(
            r"(?i)(?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token"
            r"|auth[_-]?token|password|passwd|pwd)\s*[=:]\s*['\"]?(?P<secret>[^\s'\"]{8,})"
        ),
    ),
    (  # a ``.env`` line whose name says it is a secret, including short values the rule above skips
        "env_line",
        re.compile(
            r"(?m)^(?:export[ \t]+)?[A-Z][A-Z0-9_]*"
            r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIALS?|DSN)[A-Z0-9_]*"
            r"[ \t]*=[ \t]*(?P<q>[\"']?)(?P<secret>[^\r\n]+?)(?P=q)[ \t]*$"
        ),
    ),
    ("bearer_token", re.compile(r"(?i)\bBearer[ \t]+(?P<secret>[A-Za-z0-9\-._~+/]{16,}=*)")),
]

# Public to the rest of the package (the artifact evaluators reuse the credential shapes).
_PATTERNS = _RULES

_ENTROPY_NAME = "high_entropy"
# ``sha512-<base64>`` integrity hashes are long and random but are not secrets.
_INTEGRITY = re.compile(r"^sha(?:1|256|384|512)-")
_OFF = frozenset({"0", "false", "no", "off"})
_DEFAULT_THRESHOLD = 4.5
_DEFAULT_MIN_LENGTH = 32


@dataclass(frozen=True)
class EntropyConfig:
    """The entropy rule: a token at least ``min_length`` characters long, holding letters and
    digits, whose Shannon entropy is at least ``threshold`` bits per character is treated as a
    secret. Hex digests (at most 4.0 bits per character), UUIDs and identifiers without digits
    stay below the default. Set ``FUSION_REDACT_ENTROPY=off`` to turn the rule off, and
    ``FUSION_REDACT_ENTROPY_THRESHOLD`` / ``FUSION_REDACT_ENTROPY_MIN_LENGTH`` to tune it."""

    enabled: bool = True
    threshold: float = _DEFAULT_THRESHOLD
    min_length: int = _DEFAULT_MIN_LENGTH

    @classmethod
    def from_env(cls) -> EntropyConfig:
        """The settings in the environment; a missing or invalid value keeps its default."""
        enabled = os.environ.get("FUSION_REDACT_ENTROPY", "on").strip().lower() not in _OFF
        threshold = _number(os.environ.get("FUSION_REDACT_ENTROPY_THRESHOLD"), float, 0.0)
        length = _number(os.environ.get("FUSION_REDACT_ENTROPY_MIN_LENGTH"), int, 8)
        return cls(
            enabled=enabled,
            threshold=_DEFAULT_THRESHOLD if threshold is None else threshold,
            min_length=_DEFAULT_MIN_LENGTH if length is None else int(length),
        )


def _number(raw: str | None, kind: type[float] | type[int], floor: float) -> float | None:
    if raw is None:
        return None
    try:
        value = kind(raw.strip())
    except ValueError:
        return None
    return value if value > floor or (kind is int and value >= floor) else None


@dataclass
class RedactionResult:
    """Result of redacting secrets from text."""

    text: str
    redaction_count: int = 0
    redacted_types: list[str] = field(default_factory=list)


def redact_secrets(text: str, *, entropy: EntropyConfig | None = None) -> RedactionResult:
    """Redact known secret shapes (and, unless turned off, random-looking tokens) from text.

    Returns the sanitized text and what was redacted. ``entropy`` defaults to the settings in the
    environment (see ``EntropyConfig``).
    """
    result = text
    count = 0
    types: list[str] = []

    for name, pattern in _RULES:
        result, n = _apply(pattern, result)
        if n:
            count += n
            if name not in types:
                types.append(name)

    config = EntropyConfig.from_env() if entropy is None else entropy
    if config.enabled:
        result, n = _redact_entropy(result, config)
        if n:
            count += n
            types.append(_ENTROPY_NAME)

    return RedactionResult(text=result, redaction_count=count, redacted_types=types)


def _apply(pattern: re.Pattern[str], text: str) -> tuple[str, int]:
    """Replace each match's ``secret`` group (or the whole match); count only real changes."""
    count = 0
    has_secret = "secret" in pattern.groupindex

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        if not has_secret:
            count += 1
            return _REDACTED
        if match.group("secret") == _REDACTED:  # an earlier rule got there first
            return match.group(0)
        count += 1
        start, end = match.span("secret")
        offset = match.start()
        whole = match.group(0)
        return whole[: start - offset] + _REDACTED + whole[end - offset :]

    return pattern.sub(replace, text), count


# ---------------------------------------------------------------------------------- entropy rule


@cache
def _token_pattern(min_length: int) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![\w\-+/.])[A-Za-z0-9_\-+/]{{{min_length},}}={{0,2}}(?![\w\-+/.=])"
    )


def shannon_entropy(token: str) -> float:
    """Bits of entropy per character of ``token`` (0.0 for an empty string)."""
    if not token:
        return 0.0
    size = len(token)
    return -sum(n / size * math.log2(n / size) for n in Counter(token).values())


def _looks_random(token: str, config: EntropyConfig) -> bool:
    if _INTEGRITY.match(token):
        return False
    has_digit = any(c.isdigit() for c in token)
    has_letter = any(c.isalpha() for c in token)
    return has_digit and has_letter and shannon_entropy(token) >= config.threshold


def _redact_entropy(text: str, config: EntropyConfig) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        if not _looks_random(match.group(0), config):
            return match.group(0)
        count += 1
        return _REDACTED

    return _token_pattern(config.min_length).sub(replace, text), count


# ------------------------------------------------------------------------- outbound model calls


@lru_cache(maxsize=64)
def _redact_cached(text: str, config: EntropyConfig) -> tuple[str, int]:
    """``redact_secrets`` for prompts: a panel sends the same prompt to every member, so the scan
    of a large diff is paid once."""
    found = redact_secrets(text, entropy=config)
    return found.text, found.redaction_count


def redact_request(request: ModelRequest) -> tuple[ModelRequest, int]:
    """The request with secrets removed from its system prompt, user prompt and messages, and how
    many were removed. A request with nothing to remove is returned as it is."""
    config = EntropyConfig.from_env()
    removed = 0

    def clean(text: str) -> str:
        nonlocal removed
        if not text:
            return text
        cleaned, count = _redact_cached(text, config)
        removed += count
        return cleaned

    system = clean(request.system_prompt)
    user = clean(request.user_prompt)
    messages = [m.model_copy(update={"content": clean(m.content)}) for m in request.messages]
    if not removed:
        return request, 0
    update: dict[str, object] = {"system_prompt": system, "user_prompt": user, "messages": messages}
    return request.model_copy(update=update), removed


def redact_value(value: Any) -> Any:
    """``value`` (nested dicts and lists) with secrets removed from every string in it."""
    if isinstance(value, str):
        return redact_secrets(value).text
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


def redact_error(text: str) -> str:
    """An error message that is safe to store or show: a provider often quotes the request back."""
    return redact_secrets(text).text
