"""Model output is cleaned and bounded before it reaches the host or a terminal.

A model's text ends up in Claude Code's context and, through ``fusion ask``, on a terminal. Terminal
escape sequences can hide text, rewrite earlier lines or set a window title; bidirectional
overrides make code read differently from what it is; an unbounded answer can fill the host's
context. None of that is information the caller wants, so it is removed at the one layer every
response passes through.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from fusion.mcp_server.response import present_comparison, present_run, present_stats
from fusion.mcp_server.server import create_mcp_server
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.providers.mock import MockProvider
from fusion.security.output import (
    MAX_FIELD_CHARS,
    MAX_ITEMS,
    MAX_TOTAL_CHARS,
    harden,
    strip_control,
)

ESC = "\x1b"
FORBIDDEN = set(range(0x00, 0x20)) - {0x09, 0x0A}
FORBIDDEN |= set(range(0x7F, 0xA0)) | set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))


def clean(text: str) -> bool:
    return not any(ord(c) in FORBIDDEN for c in text)


# ------------------------------------------------------------------------------ control characters


@pytest.mark.parametrize(
    ("dirty", "expected"),
    [
        (f"{ESC}[31mred{ESC}[0m text", "red text"),  # colour
        (f"a{ESC}[2K{ESC}[1Ab", "ab"),  # erase line, cursor up: rewrites earlier output
        (f"{ESC}[?1049hscreen{ESC}[?1049l", "screen"),  # alternate screen
        (f"{ESC}]0;owned\x07title", "title"),  # window title, BEL-terminated
        (f"{ESC}]0;owned{ESC}\\title", "title"),  # window title, ST-terminated
        (f"{ESC}]8;;http://evil.test{ESC}\\link{ESC}]8;;{ESC}\\", "link"),  # hyperlink
        (f"{ESC}Pdevice control{ESC}\\after", "after"),
        (f"x{ESC}cy", "xy"),  # terminal reset
        (f"a{ESC}", "a"),  # a lone escape
        ("a\x00b\x07c\x08d", "abcd"),  # NUL, bell, backspace
        ("progress 10%\rprogress 99%", "progress 10%progress 99%"),  # carriage return overwrite
        ("line1\r\nline2", "line1\nline2"),
        ("a\x9b31mb\x85c", "a31mbc"),  # 8-bit C1 controls
        ("if admin\u202e { } \u2066nested\u2069", "if admin { } nested"),  # bidi overrides
        ("tab\there\nnewline", "tab\there\nnewline"),
        ("café 日本語 🚀 ünïcödé", "café 日本語 🚀 ünïcödé"),
        ("```python\nprint('hi')\n```", "```python\nprint('hi')\n```"),
    ],
)
def test_strip_control(dirty: str, expected: str) -> None:
    assert strip_control(dirty) == expected


def test_an_unterminated_title_sequence_does_not_eat_the_rest_of_the_answer() -> None:
    tail = "x" * 5000
    out = strip_control(f"{ESC}]0;never closed{tail}")
    assert out.endswith(tail[-100:]) and len(out) > 2000


def test_strip_control_is_idempotent() -> None:
    dirty = f"{ESC}[1mbold{ESC}[0m\x00\r\n\u202etext"
    once = strip_control(dirty)
    assert strip_control(once) == once and clean(once)


# ---------------------------------------------------------------------------- structures and caps


def test_harden_cleans_every_string_in_a_nested_record_and_leaves_other_types() -> None:
    value = {
        "a": f"{ESC}[31mred",
        "n": 3,
        "f": 0.5,
        "b": True,
        "none": None,
        "list": [f"x{ESC}[0m", 7, {"deep": "\x00y"}],
        f"{ESC}[1mkey": "v",
    }
    cleaned, report = harden(value)
    assert cleaned == {
        "a": "red",
        "n": 3,
        "f": 0.5,
        "b": True,
        "none": None,
        "list": ["x", 7, {"deep": "y"}],
        "key": "v",
    }
    assert report.removed_chars > 0 and report.truncated == 0


def test_harden_leaves_clean_input_equal_and_reports_nothing() -> None:
    value = {"a": "fine", "b": ["x", "y"], "c": 1}
    cleaned, report = harden(value)
    assert cleaned == value and not report.changed


def test_one_field_is_capped_and_says_how_much_was_cut() -> None:
    cleaned, report = harden("x" * (MAX_FIELD_CHARS + 5000))
    assert len(cleaned) < MAX_FIELD_CHARS + 300
    assert "5,000 characters" in cleaned and report.truncated == 1


def test_a_response_has_a_total_budget_across_fields() -> None:
    each = MAX_FIELD_CHARS - 1000
    count = MAX_TOTAL_CHARS // each + 3
    cleaned, report = harden({f"f{i}": "y" * each for i in range(count)})
    total = sum(len(v) for v in cleaned.values())
    assert total <= MAX_TOTAL_CHARS + 300 * count
    assert report.truncated >= 1


def test_a_very_long_list_is_cut_and_counted() -> None:
    cleaned, report = harden(list(range(MAX_ITEMS + 40)))
    assert len(cleaned) == MAX_ITEMS and report.truncated == 1


def test_a_deeply_nested_value_does_not_blow_the_stack() -> None:
    value: Any = "leaf"
    for _ in range(500):
        value = [value]
    cleaned, _ = harden(value)
    assert cleaned is not None


# ------------------------------------------------------------------- the presentation functions


def output(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "r1",
        "display_markdown": f"## Answer\n{ESC}[31mUse a lock{ESC}[0m\n\u202eevil",
        "warnings": [],
        "cost_latency": {"total_cost_usd": 0.01, "total_latency_ms": 1200.0},
        "usage": {"successful_model_calls": 3},
        "routing": {"strategy": "panel-cheap"},
        "confidence": 0.8,
    }
    base.update(over)
    return base


def test_present_run_removes_control_characters_and_says_so() -> None:
    result = present_run(output(), "compact")
    assert clean(result.display_markdown) and "Use a lock" in result.display_markdown
    assert any("control characters" in w for w in result.warnings)


def test_present_run_caps_a_full_response_and_says_so() -> None:
    huge = "word " * 100_000
    raw = [{"model": "m", "text": huge}]
    result = present_run(output(display_markdown=huge, raw_outputs=raw), "full")
    assert len(result.display_markdown) < MAX_FIELD_CHARS + 300
    assert len(json.dumps(result.model_dump(), default=str)) < MAX_TOTAL_CHARS * 2
    assert any("truncated" in w for w in result.warnings)


def test_present_run_stays_silent_about_clean_output() -> None:
    result = present_run(output(display_markdown="## Answer\nfine"), "compact")
    assert result.warnings == []


def test_present_run_cleans_the_other_text_it_returns() -> None:
    dirty = output(
        warnings=[f"{ESC}[31mwarn"],
        claims=[{"text": f"{ESC}]0;t\x07claim"}],
        raw_outputs=[{"model": "m", "text": "\x00raw"}],
    )
    result = present_run(dirty, "full")
    assert result.warnings[0] == "warn"
    assert all(clean(w) for w in result.warnings)
    assert result.claims == [{"text": "claim"}]
    assert result.raw_outputs == [{"model": "m", "text": "raw"}]


def test_present_stats_and_comparison_are_cleaned_too() -> None:
    stats = present_stats({"display_markdown": f"{ESC}[1mstats", "warnings": [], "result": {}})
    assert stats.display_markdown == "stats"
    compare = present_comparison(
        {"display_markdown": f"{ESC}[1mcmp", "warnings": [], "result": {}, "evals": {}}
    )
    assert compare.display_markdown == "cmp"


# --------------------------------------------------------------------------- through the server


def strings(value: Any) -> list[str]:
    """Every string in a nested record, keys included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for k, v in value.items() for t in (*strings(k), *strings(v))]
    if isinstance(value, list):
        return [t for item in value for t in strings(item)]
    return []


async def test_an_mcp_tool_result_has_no_control_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = MockProvider.complete

    async def hostile(self: MockProvider, request: ModelRequest) -> ModelResponse:
        """Every model's text carries terminal escapes and a bidi override."""
        response = await original(self, request)
        text = f"{ESC}[2J{ESC}]0;pwned\x07{response.text}\u202e\x00"
        return response.model_copy(update={"text": text})

    monkeypatch.setattr(MockProvider, "complete", hostile)
    server = create_mcp_server(db_path=str(tmp_path / "runs.db"))
    async with Client(server) as client:
        result = await client.call_tool(
            "fusion_ask",
            {"input": {"prompt": "How should I retry a failed HTTP call?", "detail": "full"}},
        )
        run_id = result.structured_content["run_id"]
        stored = await client.read_resource(f"fusion://runs/{run_id}")
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert text and clean(text)
    found = strings(result.structured_content)
    assert found and all(clean(t) for t in found)
    record = strings(json.loads(stored[0].text))  # the stored run, read as a resource
    assert record and all(clean(t) for t in record)
