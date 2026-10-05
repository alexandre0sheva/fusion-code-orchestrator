"""The scorer interface and what every scorer shares: the answer it reads, the judge it may call.

A scorer turns an arm's answer to a task into a quality in [0, 1] using the task's ground truth.
Deterministic checks come first; a scorer calls an LLM judge only for what they cannot decide,
and only when the study names judge models. Whatever a scorer spends goes through ``ScoreEnv``'s
own ledger, so it is reported as ``eval_cost_usd`` and never counted as the arm's cost.

``ScoreResult`` is the roadmap's ``Score``: ``quality`` is its ``value``, ``details`` its
``parts`` and ``scorer`` its ``method``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from fusion.bench.spec import BenchTask, Category
from fusion.orchestration.ledger import CallGateway
from fusion.providers.base import ModelRequest
from fusion.routing.budget import PlannedCall, estimate_tokens

__all__ = [
    "JUDGE_ROLE",
    "PASS_THRESHOLDS",
    "AnswerView",
    "Evidence",
    "EvidenceItem",
    "ScoreEnv",
    "ScoreResult",
    "Scorer",
    "ScoringError",
    "ask_judge",
    "estimate_judge_call",
    "is_solved",
    "normalize",
    "segments",
    "task_text",
]

# Quality an answer needs to count as solved, per category.
PASS_THRESHOLDS: dict[Category, float] = {
    "code_review": 0.6,
    "debugging": 0.6,
    "architecture": 0.6,
    "planning": 0.6,
    "coding": 0.6,
    "frontend": 0.6,
    "performance": 0.6,
}

JUDGE_ROLE = "bench_judge"  # ``metadata["role"]`` of every judge call (the simulated judge's cue)
_ANSWER_ALLOWANCE_TOKENS = 1200  # planning assumption: how long the answer a judge reads is
_VERDICT_TOKENS = 400  # planning assumption: how long a judge's JSON verdict is


def is_solved(category: Category, quality: float | None) -> bool:
    return quality is not None and quality >= PASS_THRESHOLDS[category]


class ScoringError(RuntimeError):
    """A task this scorer cannot score (no usable truth, no judge where one is required)."""


@dataclass
class AnswerView:
    """What a scorer may look at: the arm's final answer and the claims behind it."""

    final_answer: str
    claims: list[dict[str, Any]] = field(default_factory=list)  # ClaimCluster dumps
    structured: dict[str, Any] = field(default_factory=dict)
    halted: bool = False  # the run stopped early; the answer is a diagnostic, not an attempt

    def text(self) -> str:
        """What the arm answered, for matching. The claims behind it are not added: an
        aggregator that dropped a point must not get credit for a model having raised it."""
        return self.final_answer


class EvidenceItem(BaseModel):
    """One measured fact about an answer: a test run, a timing, a visual check."""

    kind: str  # "test", "perf", "visual", "a11y", ...
    name: str
    passed: bool | None = None
    value: float | None = None
    detail: str = ""

    def line(self) -> str:
        verdict = {True: "PASS", False: "FAIL", None: ""}[self.passed]
        value = "" if self.value is None else f" = {self.value:g}"
        extra = f": {self.detail}" if self.detail else ""
        return f"[{self.kind}] {self.name}{value} {verdict}{extra}".replace("  ", " ").strip()


class Evidence(BaseModel):
    """Measurements taken from an answer's artefacts (roadmap Task 17 produces them). A scorer
    that can use them shows them to its judge; every scorer accepts them."""

    items: list[EvidenceItem] = Field(default_factory=list)

    def render(self) -> str:
        return "\n".join(item.line() for item in self.items)


@dataclass
class ScoreEnv:
    """Resources a scorer may use. ``gateway`` records its calls in a ledger of its own, so the
    money a scorer spends is kept apart from the arm's."""

    gateway: CallGateway
    judge_models: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # judge calls that failed, for the details

    def spent_usd(self) -> float:
        return self.gateway.ledger.total_cost().usd


class ScoreResult(BaseModel):
    quality: float = Field(ge=0.0, le=1.0)
    scorer: str
    details: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0  # what this score's judge calls cost; never part of the arm's cost


class Scorer(Protocol):
    name: str

    def estimate_calls(self, task: BenchTask, judges: list[str]) -> list[PlannedCall]:
        """The judge calls scoring one answer to ``task`` may make, for the planner to price
        (none for a deterministic scorer, or when no judge is named)."""
        ...

    async def score(
        self,
        task: BenchTask,
        answer: AnswerView,
        env: ScoreEnv,
        evidence: Evidence | None = None,
    ) -> ScoreResult: ...


# -- text helpers shared by the scorers ----------------------------------------------------------

_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s")


def normalize(text: str) -> str:
    """Lower case, with separators and runs of blanks reduced to single spaces."""
    return re.sub(r"[\s_\-]+", " ", text.lower()).strip()


def segments(text: str) -> list[str]:
    """The answer's items: its list entries when it has a list, else its paragraphs.

    A bullet, a heading or a blank line starts a new item and continuation lines join the item
    above; an item of fewer than three words is a label, not a statement.
    """
    items: list[tuple[bool, str]] = []  # (started by a bullet, text)
    current: list[str] = []
    bulleted = False

    def flush() -> None:
        if current:
            items.append((bulleted, " ".join(current)))
            current.clear()

    for raw in text.splitlines():
        if not raw.strip() or _HEADING.match(raw):
            flush()
        elif _BULLET.match(raw):
            flush()
            bulleted = True
            current.append(_BULLET.sub("", raw, count=1).strip())
        else:
            if not current:
                bulleted = False
            current.append(raw.strip())
    flush()
    long_enough = [(b, t) for b, t in items if len(t.split()) >= 3]
    listed = [t for b, t in long_enough if b]
    return listed if len(listed) >= 2 else [t for _, t in long_enough]


def task_text(task: BenchTask, *, files: bool = True) -> str:
    """The task as a judge should see it."""
    parts = [task.prompt]
    if task.context:
        parts.append(task.context)
    if files:
        parts.extend(f"--- {path}\n{content}" for path, content in task.files.items())
    return "\n\n".join(parts)


# -- calling a judge ---------------------------------------------------------------------------


def estimate_judge_call(task: BenchTask, alias: str, *, extra_tokens: int = 0) -> PlannedCall:
    """A judge call over the task and an answer of the assumed length."""
    tokens = estimate_tokens(task_text(task)) + _ANSWER_ALLOWANCE_TOKENS + extra_tokens
    return PlannedCall("judge", alias, tokens, _VERDICT_TOKENS)


async def ask_judge(
    env: ScoreEnv,
    alias: str,
    *,
    kind: str,
    prompt: str,
    payload: dict[str, Any],
    max_tokens: int = 1200,
) -> dict[str, Any] | None:
    """One judge call that must answer in JSON. ``None`` when the call or its JSON failed (the
    reason is added to ``env.errors``); a scorer then decides without that judge.

    ``payload`` repeats what the prompt shows as data: real providers ignore it, and the
    simulated judge reads it instead of parsing prose. It never contains ground truth.
    """
    entry = env.gateway.models.get(alias)
    if entry is None:
        env.errors.append(f"{kind}: judge model '{alias}' is not in the catalog")
        return None
    request = ModelRequest(
        model_id=entry.model_id,
        system_prompt="You are a strict, impartial evaluation judge. Answer with JSON only.",
        user_prompt=prompt,
        max_tokens=max_tokens,
        json_mode=entry.supports_json,
        metadata={"role": JUDGE_ROLE, "judge": {"kind": kind, "payload": payload}},
    )
    response = await env.gateway.call(stage="judge", alias=alias, request=request)
    if response.error:
        env.errors.append(f"{kind}: {alias} failed: {response.error}")
        return None
    data = response.parsed_json
    if data is None:
        start, end = response.text.find("{"), response.text.rfind("}") + 1
        try:
            parsed = json.loads(response.text[start:end]) if 0 <= start < end else None
        except json.JSONDecodeError:
            parsed = None
        data = parsed if isinstance(parsed, dict) else None
    if data is None:
        env.errors.append(f"{kind}: {alias} did not answer in JSON")
    return data
