"""Clean and bound model output before it reaches the host or a terminal.

Text a model wrote is untrusted. Terminal escape sequences in it can hide text, rewrite earlier
lines or set a window title; bidirectional overrides make code read differently from what it is;
and an unbounded answer can fill the host's context. ``harden`` removes the first two and caps the
third, and reports what it did so the response can say so instead of changing silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MAX_FIELD_CHARS",
    "MAX_ITEMS",
    "MAX_TOTAL_CHARS",
    "HardenReport",
    "harden",
    "strip_control",
]

# One string, one response and one list. A compact answer is far smaller (about 6,000 characters);
# these bound ``detail: full``, which returns every model's raw output.
MAX_FIELD_CHARS = 100_000
MAX_TOTAL_CHARS = 400_000
MAX_ITEMS = 500
_MAX_DEPTH = 24
_MAX_SEQUENCE = 2048  # an unterminated title or hyperlink may not swallow more than this

_ESCAPE = re.compile(
    "|".join(
        (
            r"\x1b\[[0-?]*[ -/]*[@-~]",  # CSI: colour, cursor movement, erase, screen modes
            rf"\x1b\][^\x07\x1b]{{0,{_MAX_SEQUENCE}}}(?:\x07|\x1b\\)?",  # OSC: title, hyperlink
            rf"\x1b[PX^_][^\x1b]{{0,{_MAX_SEQUENCE}}}(?:\x1b\\)?",  # DCS, SOS, PM, APC
            r"\x1b[@-Z\\-_a-z0-9=>]",  # two-byte sequences, including the terminal reset (ESC c)
        )
    )
)
# C0 controls other than tab and newline, DEL, C1 controls, and the bidirectional overrides and
# isolates ("Trojan source"). A lone escape left over from a malformed sequence goes here too.
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def strip_control(text: str) -> str:
    """``text`` without terminal escape sequences, control characters or bidi overrides.

    Newlines and tabs stay; ``\\r\\n`` becomes ``\\n`` and a lone carriage return (which makes a
    terminal overwrite the line) is removed.
    """
    cleaned = _ESCAPE.sub("", text.replace("\r\n", "\n"))
    return _CONTROL.sub("", cleaned)


@dataclass
class HardenReport:
    """What ``harden`` did."""

    removed_chars: int = 0  # control characters taken out
    truncated: int = 0  # strings and lists that were cut

    @property
    def changed(self) -> bool:
        return bool(self.removed_chars or self.truncated)


class _Budget:
    def __init__(self, total: int) -> None:
        self.left = total
        self.report = HardenReport()


def harden(value: Any) -> tuple[Any, HardenReport]:
    """A copy of ``value`` (nested dicts, lists and strings) with every string cleaned of control
    characters and capped, the whole of it held to ``MAX_TOTAL_CHARS``."""
    budget = _Budget(MAX_TOTAL_CHARS)
    return _walk(value, budget, 0), budget.report


def _walk(value: Any, budget: _Budget, depth: int) -> Any:
    if isinstance(value, str):
        return _text(value, budget)
    if depth >= _MAX_DEPTH:
        return value if not isinstance(value, dict | list) else None
    if isinstance(value, dict):
        return {_key(k, budget): _walk(v, budget, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        items = value
        if len(items) > MAX_ITEMS:
            items = items[:MAX_ITEMS]
            budget.report.truncated += 1
        return [_walk(item, budget, depth + 1) for item in items]
    return value


def _key(key: Any, budget: _Budget) -> Any:
    if not isinstance(key, str):
        return key
    cleaned = strip_control(key)
    budget.report.removed_chars += len(key) - len(cleaned)
    return cleaned


def _text(text: str, budget: _Budget) -> str:
    cleaned = strip_control(text)
    budget.report.removed_chars += len(text) - len(cleaned)
    allowed = max(min(MAX_FIELD_CHARS, budget.left), 0)
    if len(cleaned) > allowed:
        cut = len(cleaned) - allowed
        cleaned = f"{cleaned[:allowed]}\n\n[… {cut:,} characters truncated]"
        budget.report.truncated += 1
    budget.left -= min(len(cleaned), allowed)
    return cleaned
