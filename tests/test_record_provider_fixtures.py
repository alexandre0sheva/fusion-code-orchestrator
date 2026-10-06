"""The fixture recorder: it turns a real exchange into a fixture the replay tests accept.

Everything here runs against a fake upstream (``httpx.MockTransport``); the recorder itself is only
ever pointed at the real APIs by hand, with ``--max-usd``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from _provider_helpers import always
from test_providers_recorded import FIXTURES, RETRY, http_response, load, replay

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "evals" / "runners" / "record_provider_fixtures.py"


def recorder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("record_provider_fixtures", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upstream(provider: str, name: str) -> httpx.AsyncBaseTransport:
    """A fake provider API that answers with a stored fixture's HTTP exchange."""
    transport, _ = always(http_response(load(FIXTURES / provider / f"{name}.json")["http"]))
    return transport


@pytest.mark.parametrize(
    ("provider", "kind", "source"),
    [
        ("anthropic", "basic", "messages_cached"),
        ("openai", "basic", "chat_basic"),
        ("google", "basic", "generate_basic"),
        ("anthropic", "stream", "stream_basic"),
        ("openai", "stream", "stream_basic"),
        ("google", "stream", "stream_basic"),
        ("anthropic", "bad_key", "error_auth"),
        ("google", "bad_key", "error_invalid_key"),
        ("openai", "bad_key", "error_rate_limit"),
    ],
)
async def test_a_recording_is_replayable_and_agrees_with_what_the_adapter_read(
    tmp_path: Path, provider: str, kind: str, source: str
) -> None:
    mod = recorder()
    fixture = await mod.record(
        provider, kind, api_key="test-key", transport=upstream(provider, source), retry_policy=RETRY
    )
    assert fixture["provenance"].startswith("recorded ")
    stored = load(FIXTURES / provider / f"{source}.json")
    assert fixture["http"]["status"] == stored["http"]["status"]
    path = tmp_path / provider / f"recorded_{kind}.json"
    mod.write_fixture(path, fixture)
    response, _, loaded = await replay(path)
    for name, value in loaded["expect"].items():
        if name in {"error_contains"}:
            assert value in (response.error or "")
        else:
            assert getattr(response, name) == value, name


async def test_a_recorded_stream_is_stored_as_the_raw_event_text() -> None:
    fixture = await recorder().record(
        "anthropic", "stream", api_key="test-key", transport=upstream("anthropic", "stream_basic")
    )
    assert "sse" in fixture["http"] and "json" not in fixture["http"]
    assert "message_start" in fixture["http"]["sse"]
    assert fixture["request"]["stream"] is True


async def test_only_retry_headers_are_kept_and_the_key_is_never_stored() -> None:
    transport, _ = always(
        httpx.Response(
            429,
            headers={"retry-after": "3", "x-request-id": "req_1", "set-cookie": "s=1"},
            json={"error": {"message": "slow down"}},
        )
    )
    fixture = await recorder().record(
        "openai", "bad_key", api_key="sk-live-looking-key", transport=transport, retry_policy=RETRY
    )
    assert fixture["http"]["headers"] == {"retry-after": "3"}
    assert "sk-live-looking-key" not in json.dumps(fixture)


async def test_a_recording_that_contains_a_secret_is_refused() -> None:
    leaked = "sk-ant-api03-" + "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7b"
    transport, _ = always(
        httpx.Response(200, json={"content": [{"type": "text", "text": f"key {leaked}"}],
                                  "stop_reason": "end_turn",
                                  "usage": {"input_tokens": 1, "output_tokens": 1}})
    )
    mod = recorder()
    with pytest.raises(mod.RecordingError, match="secret"):
        await mod.record("anthropic", "basic", api_key="test-key", transport=transport)


def test_it_will_not_run_without_a_key_or_a_spending_limit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = recorder()
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert mod.main(["--provider", "anthropic", "--max-usd", "0.05"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    with pytest.raises(SystemExit) as stop:
        mod.main(["--provider", "anthropic"])  # --max-usd is required
    assert stop.value.code == 2


def test_the_default_output_directory_is_the_fixture_tree() -> None:
    assert recorder().DEFAULT_OUT == FIXTURES


def test_write_fixture_is_stable_json(tmp_path: Path) -> None:
    mod = recorder()
    data: dict[str, Any] = {"b": 1, "a": {"y": [1, 2], "x": "é"}}
    path = tmp_path / "x" / "f.json"
    mod.write_fixture(path, data)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n") and "é" in text
    assert json.loads(text) == data
