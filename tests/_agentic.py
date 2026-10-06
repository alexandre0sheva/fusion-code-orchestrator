"""Helpers for agentic-judge tests: a scripted tool-calling model behind a real gateway."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fusion.bench.evaluators.base import Evidence
from fusion.bench.scoring import ScoreEnv
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.routing.model_registry import ModelRegistry
from fusion.telemetry.cost import PricingRegistry

# (request, 0-based index of this judge's call) -> the action, as {"tool": ..., "args": {...}}
Policy = Callable[[ModelRequest, int], dict[str, Any]]
CALL_COST = 0.001


class ScriptedAgent(ModelProvider):
    """Plays a tool-calling judge: ``policies[model_id]`` decides each turn from the request."""

    name = "scripted-agent"

    def __init__(self, policies: dict[str, Policy], cost: float = CALL_COST) -> None:
        self.policies = policies
        self.cost = cost
        self.requests: list[ModelRequest] = []
        self.calls: dict[tuple[str, int], int] = {}  # (model id, ordering) -> calls so far

    def is_available(self) -> bool:
        return True

    def texts(self) -> str:
        """Everything the judges were ever shown, system prompts and messages."""
        parts: list[str] = []
        for r in self.requests:
            parts.append(r.system_text())
            parts.extend(m.content for m in r.messages)
        return "\n".join(parts)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        # Each (judge, ordering) conversation starts with one user message and grows by two.
        index = (len(request.messages) - 1) // 2
        action = self.policies[request.model_id](request, index)
        text = json.dumps(action)
        return ModelResponse(
            provider=self.name,
            model=request.model_id,
            text=text,
            parsed_json=action,
            input_tokens=100,
            output_tokens=20,
            latency_ms=1.0,
            cost_estimate_usd=self.cost,
        )


def agent_env(
    policies: dict[str, Policy], *, cost: float = CALL_COST
) -> tuple[ScoreEnv, ScriptedAgent]:
    """A score environment whose judges are the catalog aliases in ``policies``."""
    catalog = ModelRegistry.for_mode(use_mock=False).models
    by_id = {catalog[alias].model_id: policy for alias, policy in policies.items()}
    provider = ScriptedAgent(by_id, cost)
    providers: dict[str, ModelProvider] = {e.provider: provider for e in catalog.values()}
    gateway = CallGateway(
        ledger=RunLedger(lambda: 0.0),
        models=catalog,
        providers=providers,
        pricing=PricingRegistry(),
        truncate_prompts=False,
        temperature=0.0,
    )
    return ScoreEnv(gateway=gateway, judge_models=list(policies)), provider


def call(tool: str, **args: Any) -> dict[str, Any]:
    return {"tool": tool, "args": args}


def submit(
    scores: dict[str, Any], *, winner: str | None = None, cite: str = "E1"
) -> dict[str, Any]:
    """A well-formed verdict citing evidence ``cite``."""
    args: dict[str, Any] = {
        "scores": scores,
        "justification": f"The evidence {cite} shows what the output does, so the scores follow.",
        "evidence": [cite],
    }
    if winner is not None:
        args["winner"] = winner
    return {"tool": "submit_verdict", "args": args}


def write_output(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def evidence(kind: str = "tests", ok: bool | None = True, **extra: Any) -> Evidence:
    values: dict[str, Any] = {
        "kind": kind,
        "name": kind,
        "ok": ok,
        "metrics": {"pass_fraction": 1.0 if ok else 0.0},
        "summary": "all hidden tests pass" if ok else "2 hidden tests fail",
        "id": "E1",
    }
    values.update(extra)
    return Evidence(**values)
