"""Load YAML configuration files for routing, baselines and the model registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from fusion.config.catalog import Catalog, ModelEntry, PriceTier, load_catalog
from fusion.config.layers import (
    ConfigError,
    ResolvedConfig,
    format_validation_error,
    resolve_config,
)

__all__ = [
    "BaselineConfig",
    "BaselineEntry",
    "BudgetConfig",
    "CacheConfig",
    "DIFFICULTY_LEVELS",
    "DIFFICULTY_SIGNALS",
    "DifficultyConfig",
    "DifficultyLevel",
    "DifficultyScales",
    "EscalationPolicy",
    "EscalationTriggers",
    "PriceTier",
    "EarlyReturn",
    "FanoutConfig",
    "PromptCache",
    "ModelEntry",
    "ModelRegistryConfig",
    "RefinementConfig",
    "RoutingPoliciesConfig",
    "RoutingSection",
    "Rung",
    "VerifyConfig",
    "load_baseline",
    "load_model_registry",
    "load_routing_policies",
    "load_verify_config",
    "LibraryConfig",
    "load_library_config",
]

_CONFIG_DIR = Path(__file__).parent
_ROUTING_KEYS = frozenset({"policies", "routing", "budgets", "fanout", "refinement", "cache"})


class ModelRegistryConfig(BaseModel):
    """Full model registry loaded from YAML."""

    models: dict[str, ModelEntry]


_STRATEGY_HINT = "the panel and aggregator are set by strategies; see docs/CONFIGURATION.md"


def _reject_moved_keys(data: Any, moved: dict[str, str]) -> Any:
    """Point users of removed v0.1.0 settings at where they live now."""
    if isinstance(data, dict):
        found = [f"{key} ({hint})" for key, hint in moved.items() if key in data]
        if found:
            msg = f"no longer supported: {', '.join(found)}"
            raise ValueError(msg)
    return data


class RoutingPolicyEntry(BaseModel):
    """Per-task settings: which model judges answers and how much context is enough.

    Which models answer and aggregate is chosen by the run's strategy (``strategies`` section).
    """

    task_type: str
    judge_model: str = "gemini-flash"
    min_context_score: float = 0.3

    @model_validator(mode="before")
    @classmethod
    def _moved(cls, data: Any) -> Any:
        keys = (
            "panel_models",
            "max_panel_size",
            "high_risk_panel_models",
            "high_risk_max_panel_size",
            "budgets",
            "synthesizer_model",
        )
        return _reject_moved_keys(data, dict.fromkeys(keys, _STRATEGY_HINT))


class BudgetConfig(BaseModel):
    """Cost and latency budget defaults."""

    default_max_cost_usd: float = 1.0
    default_max_latency_ms: int = 120_000
    warn_cost_usd: float = 0.5


class EarlyReturn(BaseModel):
    """Stop waiting for stragglers once enough panelists have answered.

    ``grace_ms`` is how long stragglers get after the quorum. ``auto`` times it from the catalog's
    measured speeds: a straggler is expected to finish after its measured time to first token
    plus the answered members' output length at its decode rate. The run waits until the next
    straggler is expected, if that is within ``max_grace_ms``; it does not wait at all when every
    straggler is overdue or due later than that. When no straggler has measured speeds, ``auto``
    waits ``max_grace_ms``.
    """

    quorum: int = Field(default=2, ge=1)  # answers needed (never below min_successful_responses)
    grace_ms: float | Literal["auto"] = "auto"
    max_grace_ms: float = Field(default=1000.0, ge=0)

    @field_validator("grace_ms")
    @classmethod
    def _non_negative(cls, value: float | str) -> float | str:
        if not isinstance(value, str) and value < 0:
            msg = "grace_ms must be 'auto' or a number of milliseconds >= 0"
            raise ValueError(msg)
        return value


PromptCache = Literal["auto", "always", "off"]


def _off_is_none(value: Any) -> Any:
    """``false`` / ``off`` in YAML switches a latency control off (the same as leaving it unset)."""
    return None if value is False else value


class FanoutConfig(BaseModel):
    """Async panel fan-out controls.

    The packaged ``routing_policies.yaml`` turns ``early_return`` and ``hedge_after_ms`` on; the
    class defaults (off) apply only to a policy file that leaves them out.
    """

    max_concurrency: int = Field(default=6, ge=1)  # in-flight calls per provider
    per_model_timeout_seconds: float = Field(default=45.0, gt=0)
    global_timeout_seconds: float = Field(default=60.0, gt=0)
    min_successful_responses: int = Field(default=2, ge=1)
    cancel_on_global_timeout: bool = True
    allow_partial_results: bool = True
    early_return: EarlyReturn | None = None  # off unless set
    hedge_after_ms: float | None = Field(default=None, gt=0)  # off unless set
    # Explicit prompt-cache breakpoints (Anthropic): ``auto`` marks the shared prompt prefix only
    # when the same model is asked it more than once in a fan-out; ``always`` marks every panel
    # call (a cache write costs more than plain input); ``off`` never. OpenAI and Google cache
    # identical prefixes by themselves.
    prompt_cache: PromptCache = "auto"

    @field_validator("early_return", "hedge_after_ms", mode="before")
    @classmethod
    def _switch_off(cls, value: Any) -> Any:
        return _off_is_none(value)


class RefinementConfig(BaseModel):
    """Mixture-of-agents refinement round controls."""

    per_model_timeout_seconds: float = Field(default=45.0, gt=0)
    global_timeout_seconds: float = Field(default=60.0, gt=0)
    min_panel_size: int = Field(default=2, ge=1)
    # Skip a round when the panel already agrees at least this much (None: always refine).
    skip_above_agreement: float | None = Field(default=0.8, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def _moved(cls, data: Any) -> Any:
        hint = "refinement rounds are set by a strategy's rounds; see docs/CONFIGURATION.md"
        return _reject_moved_keys(data, dict.fromkeys(("enabled_budgets", "max_rounds"), hint))


class CacheConfig(BaseModel):
    """Cache of whole answers for identical requests (real mode; off unless enabled)."""

    enabled: bool = False
    ttl_seconds: float = Field(default=900.0, gt=0)
    max_entries: int = Field(default=128, ge=1)


DifficultyLevel = Literal["easy", "normal", "hard"]
DIFFICULTY_LEVELS: tuple[DifficultyLevel, ...] = ("easy", "normal", "hard")
# The deterministic signals ``fusion.routing.difficulty`` measures, each scaled to 0..1.
DIFFICULTY_SIGNALS = (
    "task",
    "size",
    "files",
    "hunks",
    "languages",
    "stack_depth",
    "error_type",
    "risk",
    "question",
)


class DifficultyScales(BaseModel):
    """The amount of each measured thing that counts as fully hard (its signal is then 1.0)."""

    model_config = ConfigDict(extra="forbid")

    size_tokens: int = Field(default=8000, ge=1)  # request, context and files together
    files: int = Field(default=5, ge=1)  # distinct files named or touched
    hunks: int = Field(default=10, ge=1)  # diff hunks
    languages: int = Field(default=3, ge=2)  # distinct languages by file extension
    stack_frames: int = Field(default=15, ge=1)  # frames in a stack trace


class DifficultyConfig(BaseModel):
    """How a request's difficulty is estimated (``routing.difficulty``).

    Each signal is scaled to 0..1 and the weighted mean is the score: below ``easy_below`` is
    easy, from ``hard_from`` on hard, normal in between. A score within ``ambiguity_margin`` of a
    threshold is ambiguous, and one call to ``classifier_model`` decides instead.
    """

    model_config = ConfigDict(extra="forbid")

    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "task": 0.15,
            "size": 0.15,
            "files": 0.1,
            "hunks": 0.1,
            "languages": 0.05,
            "stack_depth": 0.1,
            "error_type": 0.1,
            "risk": 0.2,
            "question": 0.05,
        }
    )
    # Base difficulty of each task type (the policy keys); a missing type counts as `default`.
    task_base: dict[str, float] = Field(
        default_factory=lambda: {
            "default": 0.1,
            "answer_eval": 0.3,
            "code_review": 0.4,
            "debugging": 0.5,
            "implementation_plan": 0.5,
            "architecture_decision": 0.7,
        }
    )
    scales: DifficultyScales = Field(default_factory=DifficultyScales)
    easy_below: float = Field(default=0.25, gt=0, lt=1)
    hard_from: float = Field(default=0.45, gt=0, lt=1)
    ambiguity_margin: float = Field(default=0.05, ge=0, lt=0.5)
    # One nano-model call settles an ambiguous estimate; null switches the call off.
    classifier_model: str | None = "claude-haiku"
    classifier_max_tokens: int = Field(default=150, ge=16, le=150)

    @model_validator(mode="after")
    def _consistent(self) -> DifficultyConfig:
        unknown = sorted(set(self.weights) - set(DIFFICULTY_SIGNALS))
        if unknown:
            msg = f"unknown difficulty signal(s) {unknown}; known: {list(DIFFICULTY_SIGNALS)}"
            raise ValueError(msg)
        if any(w < 0 for w in self.weights.values()) or sum(self.weights.values()) <= 0:
            msg = "difficulty weights must be non-negative and not all zero"
            raise ValueError(msg)
        if self.easy_below >= self.hard_from:
            msg = "difficulty.easy_below must be below difficulty.hard_from"
            raise ValueError(msg)
        return self


class Rung(BaseModel):
    """One step of the escalation ladder: a configured strategy, by name."""

    model_config = ConfigDict(extra="forbid")

    name: str
    strategy: str
    # A frontier rung runs only with allow_frontier, so a default run never pays frontier prices.
    frontier: bool = False


class EscalationTriggers(BaseModel):
    """When an answer is not enough and the next rung is asked (any one is enough)."""

    model_config = ConfigDict(extra="forbid")

    # A single model's own confidence (its self-check) below this.
    self_confidence_below: float = Field(default=0.7, ge=0, le=1)
    # Agreement below this, for a panel whose members answer the same question the same way
    # (no lenses, no approaches: those answers differ on purpose).
    agreement_below: float = Field(default=0.5, ge=0, le=1)


def _default_starts() -> dict[DifficultyLevel, int]:
    return {"easy": 0, "normal": 1, "hard": 2}


class EscalationPolicy(BaseModel):
    """The ladder ``strategy: auto`` climbs (``routing.escalation``)."""

    model_config = ConfigDict(extra="forbid")

    rungs: list[Rung] = Field(min_length=1)
    # The rung each difficulty level starts at (an index into rungs).
    start_by_level: dict[DifficultyLevel, int] = Field(default_factory=_default_starts)
    allow_frontier: bool = False
    max_rungs: int = Field(default=3, ge=1)  # rungs one run may try, the start rung included
    triggers: EscalationTriggers = Field(default_factory=EscalationTriggers)

    @model_validator(mode="after")
    def _consistent(self) -> EscalationPolicy:
        missing = [level for level in DIFFICULTY_LEVELS if level not in self.start_by_level]
        if missing:
            msg = f"escalation.start_by_level needs every level; missing {missing}"
            raise ValueError(msg)
        bad = {k: v for k, v in self.start_by_level.items() if not 0 <= v < len(self.rungs)}
        if bad:
            msg = f"escalation.start_by_level points past the {len(self.rungs)} rungs: {bad}"
            raise ValueError(msg)
        names = [r.name for r in self.rungs]
        if len(set(names)) != len(names):
            msg = "escalation.rungs: two rungs share a name"
            raise ValueError(msg)
        if all(r.frontier for r in self.rungs):
            msg = "escalation.rungs: at least one rung must not be a frontier rung"
            raise ValueError(msg)
        return self

    def climbable(self) -> list[int]:
        """Indexes of the rungs a run may use: all but a frontier rung without allow_frontier."""
        return [i for i, rung in enumerate(self.rungs) if self.allow_frontier or not rung.frontier]

    def start_for(self, level: DifficultyLevel) -> int:
        """The rung a level starts at, moved down past a frontier rung that is not allowed."""
        allowed = self.climbable()
        wanted = self.start_by_level[level]
        return max([i for i in allowed if i <= wanted], default=allowed[0])


def _default_ladder() -> EscalationPolicy:
    return EscalationPolicy(
        rungs=[
            Rung(name="nano-solo", strategy="solo-haiku-check"),
            Rung(name="nano-panel", strategy="panel-nano-auto"),
            Rung(name="mid-panel", strategy="panel-mid"),
            Rung(name="frontier", strategy="solo-opus", frontier=True),
        ]
    )


class RoutingSection(BaseModel):
    """``routing``: difficulty estimation and the escalation ladder of ``strategy: auto``."""

    model_config = ConfigDict(extra="forbid")

    difficulty: DifficultyConfig = Field(default_factory=DifficultyConfig)
    escalation: EscalationPolicy = Field(default_factory=_default_ladder)


class RoutingPoliciesConfig(BaseModel):
    """Full routing policies loaded from YAML."""

    policies: dict[str, RoutingPolicyEntry]
    routing: RoutingSection = Field(default_factory=RoutingSection)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    fanout: FanoutConfig = Field(default_factory=FanoutConfig)
    refinement: RefinementConfig = Field(default_factory=RefinementConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)


class BaselineEntry(BaseModel):
    """A frontier model that Fusion's cost and quality are compared against."""

    name: str = "Opus 5.5"
    model: str | None = None  # catalog alias; provider/model_id are resolved from it
    provider: str = "anthropic"
    model_id: str | None = "claude-opus-5-5"
    description: str = "Single frontier model baseline for comparison"
    enabled: bool = True
    estimate_strategy: str = "same_input_and_output_tokens"


class BaselineConfig(BaseModel):
    """Configured baselines; the first enabled one is the default."""

    baselines: list[BaselineEntry] = Field(default_factory=lambda: [BaselineEntry()])

    @property
    def baseline(self) -> BaselineEntry:
        for entry in self.baselines:
            if entry.enabled:
                return entry
        return self.baselines[0]


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        msg = f"Expected dict in {path}"
        raise ValueError(msg)
    return data


def load_model_registry(path: Path | None = None) -> ModelRegistryConfig:
    """Load the model registry (the catalog's models)."""
    return ModelRegistryConfig(models=load_catalog(path).models)


def load_routing_policies(path: Path | None = None) -> RoutingPoliciesConfig:
    """Load routing policies: layered configuration, or one explicit file."""
    if path is not None:
        return RoutingPoliciesConfig.model_validate(_load_yaml(path))
    resolved = resolve_config()
    section = {k: v for k, v in resolved.data.items() if k in _ROUTING_KEYS}
    try:
        return RoutingPoliciesConfig.model_validate(section)
    except ValidationError as exc:
        raise format_validation_error(exc, section_prefix="", resolved=resolved) from exc


class VerifyConfig(BaseModel):
    """``verify``: where fusion_propose_patch runs the panel's checks and the user's tests."""

    model_config = ConfigDict(extra="forbid")

    sandbox_enabled: bool = True  # false: Fusion never runs code; sandbox falls back to host
    check_timeout_s: float = Field(default=10.0, gt=0, le=600)
    tests_timeout_s: float = Field(default=120.0, gt=0, le=3600)
    mem_mb: int | None = Field(default=1024, ge=64)
    workers: int = Field(default=2, ge=1, le=16)
    max_copy_files: int = Field(default=5000, ge=1)
    max_copy_bytes: int = Field(default=64 * 1024 * 1024, ge=1)
    max_file_bytes: int = Field(default=4 * 1024 * 1024, ge=1)


def load_verify_config() -> VerifyConfig:
    """The ``verify`` section of the layered configuration."""
    resolved = resolve_config()
    try:
        return VerifyConfig.model_validate(resolved.data.get("verify") or {})
    except ValidationError as exc:
        raise format_validation_error(exc, section_prefix="verify", resolved=resolved) from exc


class LibraryConfig(BaseModel):
    """``library``: the per-project experience library (lessons learned from run feedback)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False  # opt-in: nothing is stored and no lesson is retrieved until true
    max_lessons: int = Field(default=3, ge=1, le=10)  # lessons retrieved into one panel prompt
    max_prompt_tokens: int = Field(default=300, ge=50, le=1000)  # their size in the prompt
    distil_model: str | None = None  # catalog alias; None: the task type's judge model


def load_library_config() -> LibraryConfig:
    """The ``library`` section of the layered configuration."""
    resolved = resolve_config()
    try:
        return LibraryConfig.model_validate(resolved.data.get("library") or {})
    except ValidationError as exc:
        raise format_validation_error(exc, section_prefix="library", resolved=resolved) from exc


def load_baseline(path: Path | None = None, catalog: Catalog | None = None) -> BaselineConfig:
    """Load baseline configs, resolving provider and model ID from the catalog."""
    resolved: ResolvedConfig | None = None
    if path is not None:
        raw = _load_yaml(path)
    else:
        resolved = resolve_config()
        raw = {"baselines": resolved.data.get("baselines", [])}
    resolved_catalog = catalog or load_catalog()
    entries: list[BaselineEntry] = []
    for index, item in enumerate(raw.get("baselines", [])):
        try:
            entry = BaselineEntry.model_validate(item)
        except ValidationError as exc:
            if resolved is None:
                raise
            raise format_validation_error(
                exc, section_prefix=f"baselines.{index}", resolved=resolved
            ) from exc
        if entry.model:
            if entry.model not in resolved_catalog.models:
                msg = f"baselines.{index} ({entry.name}) references unknown model '{entry.model}'"
                raise ConfigError(msg)
            model = resolved_catalog.get(entry.model)
            entry = entry.model_copy(
                update={"provider": model.provider, "model_id": model.model_id}
            )
        entries.append(entry)
    return BaselineConfig(baselines=entries) if entries else BaselineConfig()
