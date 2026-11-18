"""Simulated models: skill, price, speed and correlated mistakes, deterministic by seed.

A ``SimulatedProvider`` stands in for a real provider in studies and tests. It knows each task's
ground truth (a ``SimWorld``) and answers the way a model of a given skill would: it finds each
true point with a probability set by its skill and the task's difficulty, and sometimes asserts a
known-wrong "decoy". Whether it finds a point depends on three things added together: how hard
that point is for everyone, how hard it is for models of the same family, and its own luck. So
models make *correlated* mistakes, which is what decides whether a panel can beat one model.
Every draw is a hash of the seed, the model, the task, the point and the request's seed, so a
rerun gives identical answers, and a different repeat (a different request seed) gives different
luck. Latency, tokens and cost are reported the way a real provider would report them; run it under
``fusion.bench.virtual`` and the latencies cost no real time.

Roles it does not model (the judge) fall back to the offline mock provider's canned answers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
from collections.abc import Iterable, Mapping
from statistics import NormalDist
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from fusion.providers.base import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ServerError,
    speed_metrics,
)
from fusion.providers.mock import MockProvider
from fusion.providers.simulated_coding import coding_checks, coding_patch
from fusion.providers.simulated_judge import JUDGE_ROLE, judge_reply
from fusion.providers.simulated_tune import TUNE_COMPOSER_ROLE, TUNE_PROPOSER_ROLE, tune_reply

if TYPE_CHECKING:
    from fusion.bench.spec import BenchTask, CodingTruth, TruthPoint
    from fusion.config.catalog import Catalog, ModelEntry
    from fusion.providers.limits import ProviderLimiter

__all__ = [
    "TIER_POSITION_BIAS",
    "TIER_SKILL",
    "TIER_SPEED",
    "TIER_TOKENS",
    "SimModel",
    "SimWorld",
    "SimulatedProvider",
    "sim_models_from_catalog",
    "speed_of",
]

_NORMAL = NormalDist()
_DIFFICULTY_SHIFT = {"easy": 0.15, "medium": 0.0, "hard": -0.2}
# Skill (chance of finding an average point) and speed by the catalog's tiers.
TIER_SKILL = {"weak": 0.35, "medium": 0.55, "strong": 0.75, "frontier": 0.88}
TIER_SPEED = {
    "low": (350.0, 160.0),
    "medium": (800.0, 90.0),
    "high": (2200.0, 40.0),
}  # ttft, tok/s
TIER_TOKENS = {"weak": 500, "medium": 800, "strong": 1100, "frontier": 1400}
TIER_POSITION_BIAS = {"weak": 0.2, "medium": 0.1, "strong": 0.04, "frontier": 0.0}  # as a judge


class SimModel(BaseModel):
    """How one simulated model behaves."""

    skill: float = Field(default=0.6, ge=0.0, le=1.0)  # chance to find an average-difficulty point
    family: str = "generic"  # models of one family share part of their blind spots
    input_per_1m: float = Field(default=0.0, ge=0)
    output_per_1m: float = Field(default=0.0, ge=0)
    cached_input_per_1m: float | None = Field(default=None, ge=0)  # None: the input price
    ttft_ms: float = Field(default=800.0, ge=0)
    tokens_per_s: float = Field(default=90.0, gt=0)
    answer_tokens: int = Field(default=800, ge=1)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)  # chance a call fails outright
    # As a judge: how far it leans to the answer shown first (added to the score margin).
    position_bias: float = Field(default=0.0, ge=-1.0, le=1.0)


def speed_of(entry: ModelEntry) -> tuple[float, float]:
    """``(ttft_ms, decode tokens/s)``: the catalog's measured speed, else its latency tier's."""
    if entry.observed_ttft_ms is not None and entry.observed_decode_tps is not None:
        return entry.observed_ttft_ms, entry.observed_decode_tps
    return TIER_SPEED[entry.latency_tier]


def sim_models_from_catalog(catalog: Catalog, provider: str) -> dict[str, SimModel]:
    """A simulated twin of every catalog model of ``provider``, keyed by provider model id.

    Prices are the catalog's, so a simulated study costs what the real one would be forecast to;
    speeds are the catalog's measured ones where it has them (``speed_of``).
    """
    models: dict[str, SimModel] = {}
    for entry in catalog.models.values():
        if entry.provider != provider:
            continue
        price = entry.price_at()
        ttft, speed = speed_of(entry)
        models[entry.model_id] = SimModel(
            skill=TIER_SKILL[entry.quality_tier],
            family=provider,
            input_per_1m=price.input_per_1m if price else 0.0,
            output_per_1m=price.output_per_1m if price else 0.0,
            cached_input_per_1m=price.cached_input_per_1m if price else None,
            ttft_ms=ttft,
            tokens_per_s=speed,
            answer_tokens=TIER_TOKENS[entry.quality_tier],
            position_bias=TIER_POSITION_BIAS[entry.quality_tier],
        )
    return models


class SimWorld:
    """The tasks simulated models can answer, found by the task text inside a request."""

    def __init__(
        self,
        tasks: Iterable[BenchTask],
        *,
        seed: int = 0,
        shared_difficulty: float = 0.45,  # share of a point's difficulty common to all models
        family_difficulty: float = 0.20,  # share common to models of one family
        prompt_effects: Mapping[str, Mapping[str, float]] | None = None,
    ) -> None:
        if shared_difficulty + family_difficulty >= 1.0:
            msg = "shared_difficulty + family_difficulty must leave room for a model's own luck"
            raise ValueError(msg)
        # Longest prompt first, so a task whose prompt contains another's is matched before it.
        self.tasks = [t for _, _, t in sorted((-len(t.prompt), i, t) for i, t in enumerate(tasks))]
        self._coding: dict[str, CodingTruth] = {}
        self.seed = seed
        self.shared = shared_difficulty
        self.family = family_difficulty
        # marker text -> {category or "*": skill change} for panel calls whose prompt has it
        # (``fusion tune --mock``; see ``simulated_tune``). Empty: wording changes nothing.
        self.prompt_effects = {m: dict(e) for m, e in (prompt_effects or {}).items()}

    def prompt_bonus(self, text: str, category: str) -> float:
        """The skill change the planted prompt effects give a call whose prompt is ``text``."""
        bonus = 0.0
        for marker, effect in self.prompt_effects.items():
            if marker in text:
                bonus += effect.get(category, effect.get("*", 0.0))
        return bonus

    def task_for(self, text: str) -> BenchTask | None:
        """The task whose prompt appears in ``text`` (the longest match wins)."""
        return next((t for t in self.tasks if t.prompt in text), None)

    def coding_truth(self, task: BenchTask) -> CodingTruth:
        """The task's coding truth, parsed once."""
        from fusion.bench.spec import CodingTruth

        if task.id not in self._coding:
            self._coding[task.id] = CodingTruth.model_validate(task.truth)
        return self._coding[task.id]

    def normal(self, *parts: object) -> float:
        digest = hashlib.sha256("|".join(map(str, (self.seed, *parts))).encode()).digest()
        return random.Random(int.from_bytes(digest[:8])).gauss(0.0, 1.0)  # noqa: S311

    def uniform(self, *parts: object) -> float:
        digest = hashlib.sha256("|".join(map(str, (self.seed, *parts))).encode()).digest()
        return random.Random(int.from_bytes(digest[:8])).random()  # noqa: S311

    def hit(
        self,
        probability: float,
        *,
        model: str,
        spec: SimModel,
        task: BenchTask,
        item: str,
        request_seed: int | None,
        tag: str,
        shared: float | None = None,
    ) -> bool:
        """Does this model find ``item`` (or fall for it)? Correlated across models, repeatable."""
        a = self.shared if shared is None else shared
        b = self.family
        z = (
            math.sqrt(a) * self.normal("shared", task.id, item)
            + math.sqrt(b) * self.normal("family", spec.family, task.id, item)
            + math.sqrt(1.0 - a - b) * self.normal("own", model, task.id, item, request_seed, tag)
        )
        p = min(max(probability, 0.001), 0.999)
        return z < _NORMAL.inv_cdf(p)


def _claim(point: TruthPoint, index: int) -> dict[str, Any]:
    return {
        "id": f"c{index}",
        "text": point.text,
        "kind": point.kind,
        "severity": point.severity,
        "file": point.file,
        "line": point.line,
        "evidence": None,
    }


class SimulatedProvider(ModelProvider):
    """A provider whose models are simulated. One instance can stand in for any real provider.

    With ``prompt_cache`` it keeps a prompt cache like a real provider's: a call whose prompt
    prefix (the system prompt and the shared part of the user prompt) a finished earlier call to
    the same model already sent reads it from the cache, reported as ``cached_input_tokens`` and
    priced at the cached rate. ``automatic`` caches every prefix (OpenAI, Google), ``explicit``
    only requests that ask with ``cache_prefix`` (Anthropic). Calls running at the same time do
    not share. Simulated studies leave it ``off``: a cache shared by concurrent jobs would make a
    study's results depend on the order its jobs ran in.
    """

    def __init__(
        self,
        name: str,
        models: dict[str, SimModel],
        world: SimWorld,
        *,
        default: SimModel | None = None,
        limiter: ProviderLimiter | None = None,
        latency_scale: float = 1.0,
        prompt_cache: Literal["off", "explicit", "automatic"] = "off",
    ) -> None:
        self.name = name
        self.prompt_cache = prompt_cache
        self._cache: set[tuple[str, str]] = set()
        self.models = models
        self.world = world
        self.default = default or SimModel()
        self.limiter = limiter
        self.latency_scale = latency_scale
        self._fallback = MockProvider(latency_ms=0.0)
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls: list[tuple[str, str]] = []  # (model id, role) of every call, in order

    def is_available(self) -> bool:
        return True

    # -- the call ---------------------------------------------------------------------------

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self.limiter is None:
            return await self._complete(request)
        async with self.limiter.slot():
            return await self._complete(request)

    async def _complete(self, request: ModelRequest) -> ModelResponse:
        spec = self.models.get(request.model_id, self.default)
        role = str(request.metadata.get("role", ""))
        self.calls.append((request.model_id, role))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            prompt = request.user_prompt or " ".join(m.content for m in request.messages)
            task = self.world.task_for(prompt)
            if (
                spec.error_rate
                and self.world.uniform("error", request.model_id, request.seed, prompt[:200], role)
                < spec.error_rate
            ):
                await asyncio.sleep(spec.ttft_ms / 1000 * self.latency_scale)
                msg = f"simulated outage of {request.model_id}"
                raise ServerError(msg)
            text = self._answer(request, spec, role, prompt, task)
            parsed = _parse(text)
            input_tokens = math.ceil((len(request.system_text()) + len(prompt)) / 3.0)
            cached_tokens, prefix_key = self._cache_lookup(request, prompt)
            output_tokens = self._output_tokens(request, spec, role, task, text)
            jitter = 0.9 + 0.4 * self.world.uniform(
                "jitter", request.model_id, request.seed, prompt[-80:]
            )
            ttft = spec.ttft_ms * jitter
            latency_ms = ttft + output_tokens / spec.tokens_per_s * 1000.0 * jitter
            await asyncio.sleep(latency_ms / 1000.0 * self.latency_scale)
            if prefix_key is not None:
                self._cache.add(prefix_key)
        finally:
            self.in_flight -= 1
        decode, total = speed_metrics(output_tokens, latency_ms, ttft if request.stream else None)
        cached_rate = (
            spec.input_per_1m if spec.cached_input_per_1m is None else spec.cached_input_per_1m
        )
        cost = (
            (input_tokens - cached_tokens) * spec.input_per_1m
            + cached_tokens * cached_rate
            + output_tokens * spec.output_per_1m
        ) / 1_000_000
        return ModelResponse(
            provider=self.name,
            model=request.model_id,
            text=text,
            parsed_json=parsed,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens or None,
            actual_cost_usd=cost,
            latency_ms=latency_ms,
            ttft_ms=ttft if request.stream else None,
            decode_tokens_per_s=decode if request.stream else None,
            total_tokens_per_s=total,
            finish_reason="stop",
        )

    def _cache_lookup(
        self, request: ModelRequest, prompt: str
    ) -> tuple[int, tuple[str, str] | None]:
        """``(cached input tokens, prefix to remember)`` for a call; ``(0, None)`` when uncached."""
        mode = self.prompt_cache
        if mode == "off" or (mode == "explicit" and not request.cache_prefix):
            return 0, None
        shared = request.shared_prefix_chars
        prefix = request.system_text() + (prompt[:shared] if shared else prompt)
        key = (request.model_id, prefix)
        return (math.ceil(len(prefix) / 3.0) if key in self._cache else 0), key

    def _output_tokens(
        self, request: ModelRequest, spec: SimModel, role: str, task: BenchTask | None, text: str
    ) -> int:
        if role in {"panel", "refine", "synthesizer"} and task is not None:
            scale = 0.8 + 0.4 * self.world.uniform(
                "len", request.model_id, task.id, request.seed, role
            )
            return max(int(spec.answer_tokens * scale), len(text) // 4, 1)
        return max(len(text) // 4, 20)

    # -- what it says -----------------------------------------------------------------------

    def _answer(
        self, request: ModelRequest, spec: SimModel, role: str, prompt: str, task: BenchTask | None
    ) -> str:
        if role in (TUNE_PROPOSER_ROLE, TUNE_COMPOSER_ROLE):
            reply = tune_reply(role, request.metadata.get("tune"))
            return reply if reply is not None else self._canned(request)
        if role == JUDGE_ROLE:
            reply = judge_reply(
                request.metadata.get("judge"),
                model=request.model_id,
                spec=spec,
                world=self.world,
                seed=request.seed,
            )
            return reply if reply is not None else self._canned(request)
        if task is None or role not in {"panel", "refine", "synthesizer"}:
            return self._canned(request)
        truth = task.simulated_truth()
        shift = _DIFFICULTY_SHIFT[task.difficulty]
        model = request.model_id
        if role == "panel":
            skill = spec.skill + self.world.prompt_bonus(
                request.system_text() + "\n" + prompt, task.category
            )
            points = [
                p
                for p in truth.points
                if self.world.hit(
                    skill + shift,
                    model=model,
                    spec=spec,
                    task=task,
                    item=p.id,
                    request_seed=request.seed,
                    tag="panel",
                )
            ]
            decoys = [
                d
                for d in truth.decoys
                if self.world.hit(
                    (1.0 - min(max(skill, 0.0), 1.0)) * 0.5,
                    model=model,
                    spec=spec,
                    task=task,
                    item=d.id,
                    request_seed=request.seed,
                    tag="panel",
                    shared=0.55,
                )
            ]
            patch = self._patch(request, task, [*points, *decoys])
            extra = self._checks(request, spec, task, patch) if _wants_checks(request) else None
            return _panel_json(points, decoys, patch, extra)
        seen = prompt.replace(task.prompt, "").lower()
        if role == "refine":
            return self._refine(request, spec, task, seen)
        return self._synthesize(request, spec, task, seen)

    def _refine(self, request: ModelRequest, spec: SimModel, task: BenchTask, seen: str) -> str:
        """Keep what the model said, adopt what peers said it is persuaded by, drop some decoys."""
        truth = task.simulated_truth()
        points = [
            p
            for p in truth.points
            if _says(seen, p)
            and self.world.uniform("adopt", request.model_id, task.id, p.id, request.seed)
            < 0.4 + 0.5 * spec.skill
        ]
        decoys = [
            d
            for d in truth.decoys
            if _says(seen, d)
            and self.world.uniform("fall", request.model_id, task.id, d.id, request.seed)
            < (1.0 - spec.skill) * 0.7
        ]
        return _panel_json(points, decoys, self._patch(request, task, [*points, *decoys]))

    def _synthesize(self, request: ModelRequest, spec: SimModel, task: BenchTask, seen: str) -> str:
        """Merge what the panel said: favour points several models raised, doubt lone decoys."""
        truth = task.simulated_truth()
        sections = seen.split("## response from ")[1:] or [seen]

        def support(point: TruthPoint) -> int:
            return sum(_says(section, point) for section in sections)

        kept: list[TruthPoint] = []
        for p in truth.points:
            n = support(p)
            if not n:
                continue
            chance = min((0.55 + 0.4 * spec.skill) * (1.1 if n > 1 else 0.85), 0.99)
            if self.world.uniform("keep", request.model_id, task.id, p.id, request.seed) < chance:
                kept.append(p)
        for d in truth.decoys:
            n = support(d)
            if not n:
                continue
            chance = (1.0 - spec.skill) * 0.6 * (1.5 if n > 1 else 0.7)
            if self.world.uniform("keepd", request.model_id, task.id, d.id, request.seed) < chance:
                kept.append(d)
        texts = [p.text for p in kept]
        answer: dict[str, Any] = {
            "summary": "; ".join(texts) or "No points",
            "consensus": texts,
            "confidence": 0.7,
        }
        patch = self._patch(request, task, kept)
        if patch is not None:
            answer["patch"] = patch or None
        return json.dumps(answer)

    def _patch(self, request: ModelRequest, task: BenchTask, said: list[TruthPoint]) -> str | None:
        """The patch for a coding task (None for any other task: those answers carry no patch)."""
        if not task.expects_patch:
            return None
        return coding_patch(
            task,
            self.world.coding_truth(task),
            said,
            model=request.model_id,
            world=self.world,
            seed=request.seed,
        )

    def _checks(
        self, request: ModelRequest, spec: SimModel, task: BenchTask, patch: str | None
    ) -> dict[str, Any]:
        """verified-synth: the checks and guardrails a model writes with its patch."""
        if not patch:
            return {"checks": [], "guardrails": []}
        understood = self.world.hit(
            spec.skill + _DIFFICULTY_SHIFT[task.difficulty],
            model=request.model_id,
            spec=spec,
            task=task,
            item="checks",
            request_seed=request.seed,
            tag="checks",
        )
        truth = self.world.coding_truth(task)
        return {
            "checks": coding_checks(task, truth, patch, understood=understood),
            "guardrails": ["The tests the task ships with are not edited"],
        }

    def _canned(self, request: ModelRequest) -> str:
        """The offline mock's answer for roles (and unknown tasks) the simulation does not model."""
        personality = self._fallback._resolve_personality(request)
        return self._fallback._generate_content(request, personality)


def _says(text: str, point: TruthPoint) -> bool:
    return any(k.lower() in text for k in point.keywords)


def _wants_checks(request: ModelRequest) -> bool:
    """Does the request's answer schema ask for checks (``verified-synth``)?"""
    properties = (request.response_schema or {}).get("properties") or {}
    return "checks" in properties


def _panel_json(
    points: list[TruthPoint],
    decoys: list[TruthPoint],
    patch: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    claims = [_claim(p, i) for i, p in enumerate([*points, *decoys], 1)]
    claims.append(
        {
            "id": f"c{len(claims) + 1}",
            "text": "Run the existing test suite after the change",
            "kind": "test",
            "severity": None,
            "file": None,
            "line": None,
            "evidence": None,
        }
    )
    answer: dict[str, Any] = {
        "summary": f"{len(points) + len(decoys)} points raised",
        "claims": claims,
        "confidence": 0.7,
        "score": None,
    }
    if patch is not None:
        answer["patch"] = patch or None
    answer.update(extra or {})
    return json.dumps(answer)


def _parse(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
