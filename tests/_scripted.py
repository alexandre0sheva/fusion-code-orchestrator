"""A mock provider whose panel answers, delays and per-call costs are scripted by the test."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fusion.config.catalog import Catalog, PriceSchedule, load_catalog
from fusion.orchestration.context import PipelineContext
from fusion.orchestration.factory import Settings, build_pipeline
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.providers.mock import MockProvider
from fusion.routing.classifier import TaskType
from fusion.telemetry.cost import PricingRegistry

PROMPT = "How should I retry a failed HTTP call in httpx? Include a test strategy."
HIGH_RISK_PROMPT = "Is this production database migration safe to run on the live table?"

# Mock models stand in for the packaged panel in offline mode, in this order.
FAST, SECURITY, WEAK, JUDGE = "mock-fast", "mock-security", "mock-weak", "mock-judge"

Claim = tuple[str, str | None, str]  # kind, severity, text


def answer(*claims: Claim, summary: str = "summary") -> str:
    """A panel answer as the claims JSON a structured-output model returns."""
    return json.dumps(
        {
            "summary": summary,
            "claims": [
                {
                    "id": f"c{i}",
                    "text": text,
                    "kind": kind,
                    "severity": severity,
                    "file": None,
                    "line": None,
                    "evidence": None,
                }
                for i, (kind, severity, text) in enumerate(claims, 1)
            ],
            "confidence": 0.7,
            "score": None,
        }
    )


AGREED = answer(
    ("recommendation", None, "Retry only idempotent requests with exponential backoff"),
    ("recommendation", None, "Cap the total retry time with a deadline"),
    ("test", None, "Test the retry loop with a transport that fails twice then succeeds"),
)
OTHER = answer(
    ("risk", "low", "Retries can multiply load on a struggling server"),
    ("recommendation", None, "Use a circuit breaker instead of unbounded retries"),
)
UNRELATED = answer(("hypothesis", "med", "The connection pool is exhausted by slow handlers"))


class Scripted(MockProvider):
    """Mock answers, except where the test scripts them.

    ``answers`` maps a model id to the panel answer it gives. ``delay`` maps a model id or role to
    seconds slept before answering. ``cost`` is the cost reported per call, by role (``"*"`` for
    any); mock calls are otherwise free. ``fail`` lists model ids whose calls error out.
    """

    def __init__(
        self,
        answers: dict[str, str] | None = None,
        *,
        delay: dict[str, float] | None = None,
        cost: dict[str, float] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        super().__init__(latency_ms=0.0)
        self.answers = answers or {}
        self.delay = delay or {}
        self.cost = cost or {}
        self.fail = fail or set()
        self.requests: list[ModelRequest] = []

    def models_called(self, role: str | None = None) -> list[str]:
        return [
            r.model_id for r in self.requests if role is None or r.metadata.get("role") == role
        ]

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        role = str(request.metadata.get("role"))
        pause = self.delay.get(request.model_id, self.delay.get(role, 0.0))
        if pause:
            await asyncio.sleep(pause)
        if request.model_id in self.fail:
            return ModelResponse(
                provider="mock", model=request.model_id, error="down", error_type="Server"
            )
        scripted = self.answers.get(request.model_id)
        if scripted is not None and role in {"panel", "refine"}:
            text = scripted
            response = ModelResponse(
                provider="mock",
                model=request.model_id,
                text=text,
                parsed_json=json.loads(text),
                input_tokens=len(request.user_prompt.split()),
                output_tokens=len(text.split()),
                latency_ms=1.0,
            )
        else:
            response = await super().complete(request)
        response.cost_estimate_usd = self.cost.get(role, self.cost.get("*", 0.0))
        return response


def priced_catalog(prices: dict[str, tuple[float, float] | None] | None = None) -> Catalog:
    """The packaged catalog with the mock models given prices (USD per 1M input, output tokens).

    A model mapped to ``None`` gets no price at all.
    """
    table = {FAST: (1.0, 5.0), SECURITY: (2.0, 10.0), WEAK: (0.5, 2.0), JUDGE: (2.0, 10.0)}
    table.update(prices or {})
    catalog = load_catalog()
    models = dict(catalog.models)
    for alias, price in table.items():
        schedules = (
            [PriceSchedule(input_per_1m=price[0], output_per_1m=price[1])] if price else []
        )
        models[alias] = models[alias].model_copy(update={"prices": schedules})
    return catalog.model_copy(update={"models": models})


def pipeline(
    tmp_path: Path, provider: MockProvider | None = None, *, priced: bool = False, **kw: Any
) -> Any:
    if priced:
        kw["pricing"] = PricingRegistry(priced_catalog())
    settings = Settings(use_mock=True, db_path=str(tmp_path / "s.db"), **kw)
    return build_pipeline(settings, {"mock": provider or MockProvider(latency_ms=0.0)})


def context(prompt: str = PROMPT, **kw: Any) -> PipelineContext:
    return PipelineContext(task_type=TaskType.DEFAULT, primary_content=prompt, **kw)


def stages(result: Any) -> list[str]:
    return [r.stage for r in result.ledger.records]
