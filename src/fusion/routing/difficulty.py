"""How hard a request is, from what it contains, before any model is asked.

``strategy: auto`` starts a request on the cheapest rung of the escalation ladder that its
difficulty calls for (``fusion.orchestration.escalate``). The estimate here is deterministic: it
measures the task type, the input's size, the files and diff hunks it touches, how many languages
they are in, the depth of a stack trace and the kind of error, the risky areas it mentions
(concurrency, security, migrations, money, auth) and whether it asks a yes/no or a design question.
Each signal is scaled to 0..1 and their weighted mean, the score, is cut into easy, normal and hard
by thresholds from ``routing.difficulty``. Only a score close to a threshold is *ambiguous*; then
one call to a nano model decides (``build_difficulty_prompt``, ``parse_difficulty_reply``), made by
the caller so its cost lands in the run's ledger.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

from fusion.config.loader import DIFFICULTY_SIGNALS, DifficultyConfig, DifficultyLevel
from fusion.routing.budget import estimate_tokens
from fusion.routing.classifier import TaskType, canonical_task_key
from fusion.security.untrusted import UNTRUSTED_RULES, wrap_untrusted

__all__ = [
    "DIFFICULTY_SYSTEM_PROMPT",
    "DifficultyEstimate",
    "RequestFacts",
    "build_difficulty_prompt",
    "difficulty_schema",
    "estimate_difficulty",
    "parse_difficulty_reply",
]

# --------------------------------------------------------------------------------- the patterns

_DIFF_PATH = re.compile(r"^(?:\+\+\+ b/|--- a/|diff --git a/)(?P<path>\S+)", re.MULTILINE)
_HUNK = re.compile(r"^@@ ", re.MULTILINE)
_FRAMES = (
    re.compile(r'^\s*File "[^"]+", line \d+', re.MULTILINE),  # Python
    re.compile(r"^\s*at [\w.$<>]+\([^)]*:\d+\)", re.MULTILINE),  # Java, Kotlin
    re.compile(r"^\s*at .+:\d+:\d+\)?\s*$", re.MULTILINE),  # JavaScript, TypeScript
    re.compile(r"^\s+\S+\.go:\d+", re.MULTILINE),  # Go
)
_ERROR = re.compile(
    r"traceback|exception|\berror\b|\w+error\b|panic|stack trace|segmentation|crash",
    re.IGNORECASE,
)
_HARD_ERROR = re.compile(
    r"deadlock|race condition|\brace\b|intermittent|flaky|sporadic|heisenbug|segfault|"
    r"segmentation fault|memory leak|out of memory|corrupt|\bhangs?\b|livelock",
    re.IGNORECASE,
)
_PLAIN_ERROR = re.compile(
    r"\b(KeyError|NameError|TypeError|AttributeError|ImportError|ModuleNotFoundError|"
    r"SyntaxError|IndentationError|IndexError|ValueError|ZeroDivisionError|FileNotFoundError|"
    r"NullPointerException|ReferenceError|undefined is not)\b"
)
_RISK_AREAS: dict[str, re.Pattern[str]] = {
    "concurrency": re.compile(
        r"\b(thread|race|deadlock|mutex|lock|async|await|concurren|parallel|atomic)", re.I
    ),
    "security": re.compile(
        r"\b(security|inject|xss|csrf|secret|vulnerab|sanitiz|encrypt|crypto|exploit)", re.I
    ),
    "migrations": re.compile(r"\b(migration|migrate|alter table|backfill|schema change)", re.I),
    "money": re.compile(
        r"\b(payment|billing|invoice|refund|charge|currency|money|price|pricing)", re.I
    ),
    "auth": re.compile(r"\b(auth|login|password|session|token|permission|oauth|credential)", re.I),
}
_DESIGN = re.compile(
    r"trade-?offs?|trade offs?|\bdesign\b|architecture|should we|pros and cons|which is better|"
    r"\bcompare\b|\bversus\b|\bvs\.?\s|migrate to|\bapproach",
    re.IGNORECASE,
)
_YES_NO = re.compile(
    r"^\s*(is|are|was|were|does|do|did|can|could|will|would|has|have|should)\b[^?\n]{0,200}\?\s*$",
    re.IGNORECASE,
)
_LOOKUP = re.compile(r"^\s*(what|which|where|when|who)\b[^?\n]{0,200}\?\s*$", re.IGNORECASE)
_LANGUAGE_OF = {"tsx": "ts", "jsx": "js", "mjs": "js", "cjs": "js", "hpp": "cpp", "cc": "cpp"}


# ----------------------------------------------------------------------------------- the inputs


@dataclass(frozen=True)
class RequestFacts:
    """What comes with a request besides its text: the task type and the material it names."""

    task_type: TaskType
    context: str = ""
    snippets: Sequence[str] = field(default_factory=tuple)
    changed_files: Sequence[str] = field(default_factory=tuple)


class DifficultyEstimate(BaseModel):
    """How hard a request is: a level, the signals behind it and how sure the estimate is."""

    level: DifficultyLevel
    signals: dict[str, float]
    # 0..1: distance from the nearest threshold, relative to the ambiguity margin; below 0.5
    # the estimate is ambiguous.
    confidence: float = Field(ge=0.0, le=1.0)
    score: float = 0.0  # the weighted mean of the signals
    source: Literal["signals", "classifier"] = "signals"
    note: str = ""  # what the classifier said, or why it could not decide

    @property
    def ambiguous(self) -> bool:
        return self.confidence < 0.5

    def describe(self) -> str:
        """One line for the run record: level, score and the strongest signals."""
        top = sorted(
            ((name, value) for name, value in self.signals.items() if value > 0),
            key=lambda item: -item[1],
        )[:3]
        shown = ", ".join(f"{name} {value:.2f}" for name, value in top) or "no signal"
        how = "classifier call" if self.source == "classifier" else "signals"
        return f"{self.level} (score {self.score:.2f} from {how}; {shown})"


# ---------------------------------------------------------------------------------- the signals


def _files(text: str, facts: RequestFacts) -> set[str]:
    named = {f for f in facts.changed_files if f}
    named |= {m.group("path") for m in _DIFF_PATH.finditer(text) if m.group("path") != "/dev/null"}
    return named


def _languages(files: set[str]) -> int:
    found = set()
    for name in files:
        suffix = PurePosixPath(name).suffix.lower().lstrip(".")
        if suffix:
            found.add(_LANGUAGE_OF.get(suffix, suffix))
    return len(found)


def _frames(text: str) -> int:
    return max((len(p.findall(text)) for p in _FRAMES), default=0)


def _error_type(text: str) -> float:
    if _HARD_ERROR.search(text):
        return 1.0
    if not _ERROR.search(text):
        return 0.0
    return 0.2 if _PLAIN_ERROR.search(text) else 0.5


def _question(request: str) -> float:
    text = request.strip()
    if _DESIGN.search(text):
        return 1.0
    if _YES_NO.match(text):
        return 0.0
    if _LOOKUP.match(text):
        return 0.25
    return 0.5


def _scaled(value: float, scale: float) -> float:
    return round(min(1.0, value / scale), 4) if scale > 0 else 0.0


def measure_signals(
    request: str, facts: RequestFacts, config: DifficultyConfig
) -> dict[str, float]:
    """Every signal scaled to 0..1; deterministic, no model call."""
    material = "\n".join([request, facts.context, *facts.snippets])
    scales = config.scales
    files = _files(material, facts)
    n_files = len(files) or len([s for s in facts.snippets if s.strip()])
    key = canonical_task_key(facts.task_type)
    risky = sum(1 for pattern in _RISK_AREAS.values() if pattern.search(material))
    return {
        "task": config.task_base.get(key, config.task_base.get("default", 0.0)),
        "size": _scaled(estimate_tokens(material), scales.size_tokens),
        "files": _scaled(n_files, scales.files),
        "hunks": _scaled(len(_HUNK.findall(material)), scales.hunks),
        "languages": _scaled(max(_languages(files) - 1, 0), scales.languages - 1),
        "stack_depth": _scaled(_frames(material), scales.stack_frames),
        "error_type": _error_type(material),
        "risk": _scaled(risky, 2),
        "question": _question(request),
    }


def _level(score: float, config: DifficultyConfig) -> DifficultyLevel:
    if score < config.easy_below:
        return "easy"
    return "hard" if score >= config.hard_from else "normal"


def estimate_difficulty(
    request: str, context: RequestFacts, config: DifficultyConfig | None = None
) -> DifficultyEstimate:
    """The request's difficulty from its signals alone (see the module docstring)."""
    config = config or DifficultyConfig()
    signals = measure_signals(request, context, config)
    total = sum(config.weights.get(name, 0.0) for name in DIFFICULTY_SIGNALS)
    score = sum(signals[name] * config.weights.get(name, 0.0) for name in signals) / total
    distance = min(abs(score - config.easy_below), abs(score - config.hard_from))
    margin = config.ambiguity_margin
    confidence = 1.0 if margin == 0 else min(1.0, distance / (2 * margin))
    return DifficultyEstimate(
        level=_level(score, config),
        signals=signals,
        confidence=round(confidence, 3),
        score=round(score, 4),
    )


# ------------------------------------------------------------------------------- the classifier

DIFFICULTY_SYSTEM_PROMPT = (
    "You rate how hard a software engineering request is for a small, fast language model. "
    "You do not answer the request. Return only valid JSON."
)
_REQUEST_CHARS = 6000  # the classifier reads the start of the request; it rates, not answers


def difficulty_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "level": {"type": "string", "enum": ["easy", "normal", "hard"]},
            "why": {"type": "string"},
        },
        "required": ["level", "why"],
        "additionalProperties": False,
    }


def build_difficulty_prompt(request: str, task_type: TaskType) -> str:
    """Ask one nano model whether a request is easy, normal or hard."""
    key = canonical_task_key(task_type)
    text = request if len(request) <= _REQUEST_CHARS else request[:_REQUEST_CHARS] + "\n[...]"
    return "\n".join(
        [
            f"Rate how hard this {key} request is for a small, fast model to answer well.",
            UNTRUSTED_RULES,
            f"\n## Request\n{wrap_untrusted(text)}",
            "\n## Levels",
            "- easy: one short, well-known answer; little code; nothing risky.",
            "- normal: some reasoning over the given code or a few files; ordinary risk.",
            "- hard: many files or steps, subtle failures (concurrency, security, data loss, "
            "money, auth), or a design decision with real trade-offs.",
            '\nReturn JSON: {"level": "easy" | "normal" | "hard", "why": one short sentence}.',
        ]
    )


def parse_difficulty_reply(text: str) -> DifficultyLevel | None:
    """The level a classifier reply names, or None when it names none of the three."""
    start, end = text.find("{"), text.rfind("}") + 1
    if not 0 <= start < end:
        return None
    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError:
        return None
    level = data.get("level") if isinstance(data, dict) else None
    if isinstance(level, str) and (found := level.strip().lower()) in ("easy", "normal", "hard"):
        return found  # type: ignore[return-value]
    return None
