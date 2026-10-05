"""Shadow baseline A/B comparison.

When enabled, the same sanitized task prompt Fusion answered is also sent to
the configured frontier baseline model, and a blind pairwise judge (answers in
randomized order, unlabeled) picks a winner. Results are stored so cumulative
win-rate and real baseline cost/latency can be reported.

Shadow comparisons are measurement overhead: their cost is tracked separately
and never counted as Fusion cost. Any failure degrades to a warning; the main
run always succeeds independently.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from fusion.config.loader import BaselineEntry, ModelEntry, load_baseline
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.telemetry.cost import PricingRegistry

if TYPE_CHECKING:
    from fusion.orchestration.ledger import CallGateway

_VALID_MODES = {"off", "sampled", "always"}


def shadow_mode() -> str:
    """Read FUSION_SHADOW_MODE from the environment (off | sampled | always)."""
    mode = os.environ.get("FUSION_SHADOW_MODE", "off").strip().lower()
    return mode if mode in _VALID_MODES else "off"


def shadow_sample_rate() -> float:
    """Read FUSION_SHADOW_SAMPLE_RATE (0..1, default 0.2) for sampled mode."""
    raw = os.environ.get("FUSION_SHADOW_SAMPLE_RATE", "0.2")
    try:
        return min(max(float(raw), 0.0), 1.0)
    except ValueError:
        return 0.2


def should_run_shadow(explicit: bool | None = None, *, rng: random.Random | None = None) -> bool:
    """Decide whether this run gets a shadow comparison.

    An explicit per-call flag overrides the environment mode in both directions.
    """
    if explicit is not None:
        return explicit
    mode = shadow_mode()
    if mode == "always":
        return True
    if mode == "sampled":
        return (rng or random).random() < shadow_sample_rate()
    return False


class ShadowComparison(BaseModel):
    """Outcome of one shadow A/B comparison."""

    ran: bool = False
    baseline_model: str = ""
    baseline_name: str = ""
    judge_model: str = ""
    winner: str = "error"  # fusion | baseline | tie | error
    fusion_score: float | None = None
    baseline_score: float | None = None
    judge_reason: str = ""
    fusion_cost_usd: float | None = None
    baseline_cost_usd: float | None = None
    baseline_cost_known: bool = False
    fusion_latency_ms: float | None = None
    baseline_latency_ms: float | None = None
    baseline_answer: str = ""
    warnings: list[str] = Field(default_factory=list)


def _build_blind_judge_prompt(task: str, answer_1: str, answer_2: str) -> str:
    return (
        "You are comparing two anonymous answers to the same task. You do not "
        "know which system produced which answer. Judge only on quality: "
        "correctness, groundedness, specificity, actionability, and risk awareness.\n\n"
        f"## Task\n{task}\n\n"
        f"## Answer 1\n{answer_1}\n\n"
        f"## Answer 2\n{answer_2}\n\n"
        "Return ONLY valid JSON with keys: "
        '{"winner": "1" | "2" | "tie", '
        '"answer_1_score": float 0-1, "answer_2_score": float 0-1, '
        '"reason": "one sentence"}'
    )


def _parse_judge_verdict(content: str) -> dict[str, Any] | None:
    try:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(content[start:end])
            if isinstance(parsed, dict) and "winner" in parsed:
                return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return None


@dataclass
class BaselineCall:
    """The shadow baseline's own answer, obtained before there is anything to compare it with."""

    entry: BaselineEntry
    response: ModelResponse | None = None
    warnings: list[str] = field(default_factory=list)


async def call_shadow_baseline(
    *,
    task_prompt: str,
    system_prompt: str,
    providers: dict[str, ModelProvider],
    baseline: BaselineEntry | None = None,
    timeout_seconds: float = 90.0,
    gateway: CallGateway | None = None,
) -> BaselineCall:
    """Ask the real baseline the task. It needs only the prompt, so a pipeline can start it at
    the same moment as the panel and collect it when Fusion's answer is ready."""
    entry = baseline or load_baseline().baseline
    call = BaselineCall(entry=entry)
    provider = providers.get(entry.provider)
    if provider is None or not entry.model_id:
        call.warnings.append(f"Shadow baseline skipped: provider '{entry.provider}' unavailable")
        return call
    started = time.perf_counter()
    request = ModelRequest(
        model_id=entry.model_id,
        system_prompt=system_prompt,
        user_prompt=task_prompt,
        max_tokens=8192,
        timeout=timeout_seconds,
        metadata={"role": "shadow_baseline"},
    )
    if gateway is not None:
        response = await gateway.call(
            stage="shadow_baseline",
            alias=entry.model or entry.model_id,
            request=request,
            provider_name=entry.provider,
        )
    else:
        response = await provider.safe_complete(request)
    if response.latency_ms <= 0:
        response.latency_ms = (time.perf_counter() - started) * 1000
    if response.error or not response.content.strip():
        call.warnings.append(f"Shadow baseline call failed: {response.error or 'empty response'}")
        return call
    call.response = response
    return call


async def finish_shadow_comparison(
    *,
    baseline_call: BaselineCall,
    task_prompt: str,
    fusion_answer: str,
    fusion_cost_usd: float | None,
    fusion_latency_ms: float | None,
    registry_models: dict[str, ModelEntry],
    providers: dict[str, ModelProvider],
    judge_model_alias: str,
    pricing: PricingRegistry | None = None,
    timeout_seconds: float = 90.0,
    rng: random.Random | None = None,
    gateway: CallGateway | None = None,
) -> ShadowComparison:
    """Judge Fusion's answer against the baseline's, blind, and build the comparison."""
    entry = baseline_call.entry
    result = ShadowComparison(
        baseline_model=entry.model_id or "",
        baseline_name=entry.name,
        judge_model=judge_model_alias,
        fusion_cost_usd=fusion_cost_usd,
        fusion_latency_ms=fusion_latency_ms,
        warnings=list(baseline_call.warnings),
    )
    baseline_response = baseline_call.response
    if baseline_response is None:
        return result

    result.ran = True
    result.baseline_answer = baseline_response.content
    result.baseline_latency_ms = baseline_response.latency_ms
    baseline_cost = (pricing or PricingRegistry()).estimate_response_cost(baseline_response)
    result.baseline_cost_usd = baseline_cost.amount_usd
    result.baseline_cost_known = baseline_cost.known

    judge_entry = registry_models.get(judge_model_alias)
    judge_provider = providers.get(judge_entry.provider) if judge_entry else None
    if judge_entry is None or judge_provider is None:
        result.warnings.append(
            f"Shadow judge unavailable ({judge_model_alias}); no winner recorded"
        )
        return result

    # Randomize presentation order so the judge cannot favor a position.
    fusion_first = (rng or random).random() < 0.5
    answer_1, answer_2 = (
        (fusion_answer, baseline_response.content)
        if fusion_first
        else (baseline_response.content, fusion_answer)
    )
    judge_request = ModelRequest(
        model_id=judge_entry.model_id,
        system_prompt="You are an impartial evaluation judge. Return only valid JSON.",
        user_prompt=_build_blind_judge_prompt(task_prompt, answer_1, answer_2),
        max_tokens=1024,
        json_mode=judge_entry.supports_json,
        timeout=timeout_seconds,
        metadata={"role": "shadow_judge"},
    )
    if gateway is not None:
        judge_response = await gateway.call(
            stage="shadow_judge", alias=judge_model_alias, request=judge_request
        )
    else:
        judge_response = await judge_provider.safe_complete(judge_request)
    verdict = (
        judge_response.parsed_json
        if judge_response.parsed_json and "winner" in judge_response.parsed_json
        else _parse_judge_verdict(judge_response.content)
    )
    if judge_response.error or verdict is None:
        result.warnings.append(
            f"Shadow judge failed: {judge_response.error or 'unparseable verdict'}"
        )
        return result

    raw_winner = str(verdict.get("winner", "tie")).strip().lower()
    try:
        score_1 = float(verdict.get("answer_1_score", 0.5))
        score_2 = float(verdict.get("answer_2_score", 0.5))
    except (TypeError, ValueError):
        score_1 = score_2 = 0.5
    fusion_position = "1" if fusion_first else "2"
    if raw_winner == "tie":
        result.winner = "tie"
    elif raw_winner in {"1", "2"}:
        result.winner = "fusion" if raw_winner == fusion_position else "baseline"
    else:
        result.warnings.append(f"Shadow judge returned unknown winner '{raw_winner}'")
        return result
    result.fusion_score = score_1 if fusion_first else score_2
    result.baseline_score = score_2 if fusion_first else score_1
    result.judge_reason = str(verdict.get("reason", ""))
    return result


async def run_shadow_comparison(
    *,
    task_prompt: str,
    system_prompt: str,
    fusion_answer: str,
    fusion_cost_usd: float | None,
    fusion_latency_ms: float | None,
    registry_models: dict[str, ModelEntry],
    providers: dict[str, ModelProvider],
    judge_model_alias: str,
    pricing: PricingRegistry | None = None,
    baseline: BaselineEntry | None = None,
    timeout_seconds: float = 90.0,
    rng: random.Random | None = None,
    gateway: CallGateway | None = None,
) -> ShadowComparison:
    """Call the real baseline on the same task and judge both answers blind (one after the other).

    Pipelines use ``call_shadow_baseline`` and ``finish_shadow_comparison`` separately so the
    baseline call overlaps the panel; this is the two steps in sequence.
    """
    baseline_call = await call_shadow_baseline(
        task_prompt=task_prompt,
        system_prompt=system_prompt,
        providers=providers,
        baseline=baseline,
        timeout_seconds=timeout_seconds,
        gateway=gateway,
    )
    return await finish_shadow_comparison(
        baseline_call=baseline_call,
        task_prompt=task_prompt,
        fusion_answer=fusion_answer,
        fusion_cost_usd=fusion_cost_usd,
        fusion_latency_ms=fusion_latency_ms,
        registry_models=registry_models,
        providers=providers,
        judge_model_alias=judge_model_alias,
        pricing=pricing,
        timeout_seconds=timeout_seconds,
        rng=rng,
        gateway=gateway,
    )
