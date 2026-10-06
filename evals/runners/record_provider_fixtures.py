#!/usr/bin/env python3
"""Record real provider responses as wire fixtures for tests/test_providers_recorded.py.

    uv run python evals/runners/record_provider_fixtures.py --max-usd 0.05
    uv run python evals/runners/record_provider_fixtures.py \\
        --provider google --kinds basic,stream --max-usd 0.02

This calls the real APIs, so it needs the provider's key in the environment and an explicit
``--max-usd``. A recording is one tiny prompt (a short answer, about a cent at most); ``bad_key``
sends an invalid key and costs nothing. Each paid call is checked against and added to the
roadmap's live-spend ledger (``bench-results/spend.json``). Nothing is recorded that contains a
secret, and only the retry headers are kept. Review `expect` (it records what the adapter made of
the response) and the diff before committing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from fusion.bench.spend import SpendCapError, default_ledger
from fusion.config.catalog import ModelEntry, load_catalog
from fusion.providers.anthropic import AnthropicProvider
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse, close_providers
from fusion.providers.google import GoogleProvider
from fusion.providers.http_utils import RetryPolicy
from fusion.providers.openai import OpenAIProvider
from fusion.security.redaction import EntropyConfig, redact_secrets
from fusion.telemetry.cost import PricingRegistry

DEFAULT_OUT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "providers"
PROMPT = "How should I retry a failed HTTP call?"
MAX_TOKENS = 256
KINDS = ("basic", "stream", "bad_key")
PAID_ESTIMATE_USD = 0.01  # one short answer, generously
PROVIDERS: dict[str, tuple[type[ModelProvider], str, str]] = {
    # name: (adapter, key variable, catalog alias to ask)
    "anthropic": (AnthropicProvider, "ANTHROPIC_API_KEY", "claude-haiku"),
    "openai": (OpenAIProvider, "OPENAI_API_KEY", "gpt-luna"),
    "google": (GoogleProvider, "GOOGLE_API_KEY", "gemini-flash"),
}
NOTES = {
    "basic": "A plain successful completion.",
    "stream": "The same request streamed as server-sent events.",
    "bad_key": "An invalid key: how this provider reports authentication failure.",
}
_KEPT_HEADERS = ("retry-after", "retry-after-ms")
_DROPPED_ON_REPLAY = {"content-encoding", "content-length", "transfer-encoding"}


class RecordingError(RuntimeError):
    """A recording that must not be kept."""


class Tape(httpx.AsyncBaseTransport):
    """Passes requests to a real transport and remembers the last response it returned."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.last: tuple[int, httpx.Headers, bytes] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        body = await response.aread()
        self.last = (response.status_code, response.headers, body)
        headers = [
            (k, v) for k, v in response.headers.items() if k.lower() not in _DROPPED_ON_REPLAY
        ]
        return httpx.Response(response.status_code, headers=headers, content=body)

    async def aclose(self) -> None:
        await self.inner.aclose()


def _expect(response: ModelResponse, status: int) -> dict[str, Any]:
    """What the adapter made of the response: the claim the replay test then holds it to."""
    if not response.ok:
        return {
            "error_type": response.error_type,
            "error_contains": f"error {status}",
            "retries": response.retries,
        }
    expect: dict[str, Any] = {"text": response.text}
    for name in (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "finish_reason",
    ):
        value = getattr(response, name)
        if value is not None:
            expect[name] = value
    return expect


async def record(
    provider: str,
    kind: str,
    *,
    api_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
    retry_policy: RetryPolicy | None = None,
    on_response: Callable[[ModelResponse], None] | None = None,
) -> dict[str, Any]:
    """Ask ``provider`` the recording prompt and return the exchange as a fixture.

    ``kind`` is ``basic``, ``stream`` or ``bad_key`` (the key is replaced by an invalid one).
    ``transport`` is the real network by default; tests pass a fake upstream.
    """
    adapter_class, _, alias = PROVIDERS[provider]
    entry = load_catalog().models[alias]
    tape = Tape(transport or httpx.AsyncHTTPTransport())
    key = "invalid-key-for-recording" if kind == "bad_key" else api_key
    options: dict[str, Any] = {"api_key": key, "transport": tape}
    if retry_policy is not None:
        options["retry_policy"] = retry_policy
    adapter = adapter_class(**options)
    stream = kind == "stream"
    try:
        response = await adapter.safe_complete(
            ModelRequest(
                model_id=entry.model_id, user_prompt=PROMPT, max_tokens=MAX_TOKENS, stream=stream
            )
        )
    finally:
        await close_providers({provider: adapter})
    if on_response is not None:
        on_response(response)
    if tape.last is None:
        raise RecordingError(f"{provider} was never reached: {response.error}")
    status, headers, body = tape.last
    text = body.decode("utf-8", errors="replace")
    _refuse_secrets(text, api_key)
    http: dict[str, Any] = {"status": status}
    if kept := {name: headers[name] for name in _KEPT_HEADERS if name in headers}:
        http["headers"] = kept
    if stream:
        http["sse"] = text
    else:
        try:
            http["json"] = json.loads(text)
        except json.JSONDecodeError as exc:
            msg = f"{provider} answered {status} with something that is not JSON"
            raise RecordingError(msg) from exc
    return {
        "provenance": (
            f"recorded {date.today()} from {entry.model_id} by "
            "evals/runners/record_provider_fixtures.py (review `expect` before committing)"
        ),
        "note": NOTES[kind],
        "alias": alias,
        "request": {"user_prompt": PROMPT, "max_tokens": MAX_TOKENS, "stream": stream},
        "http": http,
        "expect": _expect(response, status),
    }


def _refuse_secrets(text: str, api_key: str) -> None:
    # Shape rules only: opaque base64 such as a thinking signature is not a secret.
    found = redact_secrets(text, entropy=EntropyConfig(enabled=False))
    if found.redaction_count or (len(api_key) >= 8 and api_key in text):
        raise RecordingError("the response contains a secret, so it was not recorded")


def write_fixture(path: Path, fixture: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--provider", choices=[*PROVIDERS, "all"], default="all")
    parser.add_argument(
        "--kinds", default=",".join(KINDS), help="comma-separated: basic,stream,bad_key"
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--max-usd", type=float, required=True, help="most this run may spend on paid recordings"
    )
    args = parser.parse_args(argv)
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    if unknown := [k for k in kinds if k not in KINDS]:
        print(f"unknown kind(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    names = list(PROVIDERS) if args.provider == "all" else [args.provider]
    keys = {n: os.environ.get(PROVIDERS[n][1], "") for n in names}
    if missing := [PROVIDERS[n][1] for n in names if not keys[n]]:
        print(f"set {', '.join(missing)} to record", file=sys.stderr)
        if args.provider != "all" or len(missing) == len(names):
            return 2
        names = [n for n in names if keys[n]]
    paid = [(n, k) for n in names for k in kinds if k != "bad_key"]
    estimate = PAID_ESTIMATE_USD * len(paid)
    if estimate > args.max_usd:
        print(
            f"{len(paid)} paid recordings are estimated at ${estimate:.2f}, over --max-usd",
            file=sys.stderr,
        )
        return 2
    ledger = default_ledger()
    try:
        ledger.check(estimate, "provider fixture recording")
    except SpendCapError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    pricing = PricingRegistry()

    async def run() -> None:
        for name in names:
            entry = load_catalog().models[PROVIDERS[name][2]]
            for kind in kinds:

                def spend(
                    response: ModelResponse,
                    *,
                    name: str = name,
                    kind: str = kind,
                    entry: ModelEntry = entry,
                ) -> None:
                    if kind != "bad_key":
                        cost = pricing.estimate_response_cost(response, entry).amount_usd
                        ledger.append(
                            cost or PAID_ESTIMATE_USD,
                            task="task-25",
                            purpose=f"record {name} {kind} fixture",
                        )

                fixture = await record(name, kind, api_key=keys[name], on_response=spend)
                target = args.out / name / f"recorded_{kind}.json"
                write_fixture(target, fixture)
                print(f"recorded {target}")

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
