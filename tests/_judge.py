"""Helpers for scorer tests: scripted judges behind a real gateway, and small tasks, all offline."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fusion.bench.scoring import ScoreEnv
from fusion.bench.spec import BenchTask
from fusion.config.catalog import ModelEntry
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.routing.model_registry import ModelRegistry
from fusion.telemetry.cost import PricingRegistry

Reply = Callable[[str, dict[str, Any]], dict[str, Any] | None]
CALL_COST = 0.002


class ScriptedJudge(ModelProvider):
    """Answers every judge call by calling ``replies[model_id](kind, payload)``.

    A reply of None is a failed call. Every request is kept in ``requests``.
    """

    name = "scripted"

    def __init__(self, replies: dict[str, Reply]) -> None:
        self.replies = replies
        self.requests: list[ModelRequest] = []

    def is_available(self) -> bool:
        return True

    def kinds(self) -> list[str]:
        return [str(r.metadata["judge"]["kind"]) for r in self.requests]

    def payloads(self, kind: str) -> list[dict[str, Any]]:
        return [r.metadata["judge"]["payload"] for r in self.requests if _kind(r) == kind]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        judge = request.metadata["judge"]
        reply = self.replies[request.model_id](judge["kind"], judge["payload"])
        if reply is None:
            return ModelResponse(
                provider=self.name, model=request.model_id, error="down", error_type="Server"
            )
        return ModelResponse(
            provider=self.name,
            model=request.model_id,
            text=json.dumps(reply),
            parsed_json=reply,
            input_tokens=100,
            output_tokens=20,
            latency_ms=1.0,
            cost_estimate_usd=CALL_COST,
        )


def _kind(request: ModelRequest) -> str:
    return str(request.metadata["judge"]["kind"])


def models() -> dict[str, ModelEntry]:
    return ModelRegistry.for_mode(use_mock=False).models


def score_env(replies: dict[str, Reply] | None = None) -> tuple[ScoreEnv, ScriptedJudge]:
    """An environment whose judges are the catalog aliases in ``replies`` (alias -> reply
    function). Their model ids are what the provider dispatches on."""
    catalog = models()
    by_id = {catalog[alias].model_id: fn for alias, fn in (replies or {}).items()}
    provider = ScriptedJudge(by_id)
    providers: dict[str, ModelProvider] = {e.provider: provider for e in catalog.values()}
    gateway = CallGateway(
        ledger=RunLedger(lambda: 0.0),
        models=catalog,
        providers=providers,
        pricing=PricingRegistry(),
        truncate_prompts=False,
        temperature=0.0,
    )
    return ScoreEnv(gateway=gateway, judge_models=list(replies or {})), provider


def make_task(category: str, truth: dict[str, Any], **extra: Any) -> BenchTask:
    values: dict[str, Any] = {
        "id": "t1",
        "category": category,
        "prompt": "Review the change to the data layer before it ships.",
        "files": {},
        "truth": truth,
    }
    values.update(extra)
    return BenchTask.model_validate(values)
