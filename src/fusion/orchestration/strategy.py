"""Strategies (declarative arms) and the two run modes.

A strategy is data: who answers, for how many rounds, who aggregates (a model, or no model at
all), whether a judge scores the answers, whether cheap models go first (a cascade) and what the
run may cost. Baselines in a benchmark and Fusion itself are both strategies, so they share one code
path and one accounting. Strategies come from the ``strategies`` config section; the legacy
``budget`` argument of the tools resolves to a strategy through ``budget_strategies``.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from fusion.config.layers import ConfigError, format_validation_error, resolve_config
from fusion.config.loader import EarlyReturn, EscalationPolicy, FanoutConfig
from fusion.routing.budget import BudgetLevel

if TYPE_CHECKING:
    from fusion.orchestration.approaches import ApproachBook
    from fusion.orchestration.lenses import LensBook
    from fusion.routing.model_registry import ModelRegistry

__all__ = [
    "LENS_AGGREGATORS",
    "CODE_AGGREGATORS",
    "MODEL_AGGREGATORS",
    "SELECT_AGGREGATORS",
    "MODE_SETTINGS",
    "BenchmarkOnlyError",
    "CascadeSpec",
    "EscalationOverride",
    "FanoutOverride",
    "Mode",
    "ModeSettings",
    "PanelMember",
    "SkipAggregator",
    "Strategy",
    "StrategyBook",
    "load_strategy_book",
    "member_overrides",
    "mock_strategy_book",
    "may_run_code",
]

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
_STRATEGY_KEYS = ("strategies", "budget_strategies", "strategy_aliases")
AggregatorName = Literal[
    "auto",
    "llm",
    "vote",
    "best_of",
    "digest",
    "verified",
    "union",
    "union+check",
    "select",
    "select+patch",
    "verified-synth",
]
# Aggregators that make no model call, so they take no aggregator_model.
NO_MODEL_AGGREGATORS = frozenset({"vote", "best_of", "digest", "verified", "union"})
# Aggregators that call a model (aggregator_model, else the catalog's synthesizer). `auto` is
# planned as if it did: it becomes union (no call) or select+patch (one call) per task.
MODEL_AGGREGATORS = frozenset(
    {"auto", "llm", "union+check", "select", "select+patch", "verified-synth"}
)
# Aggregators that keep what each lens found. vote, best_of and verified keep only what several
# answers share, or one whole answer, so they would throw away every lens's own findings. `auto`
# lenses the task types whose answer is a list of findings and gives approaches to the others.
LENS_AGGREGATORS = frozenset({"auto", "llm", "digest", "union", "union+check"})
# Aggregators that pick ONE whole answer among rivals, so members get approaches, not lenses.
# verified-synth picks by running checks and asks the selector only on a tie.
SELECT_AGGREGATORS = frozenset({"select", "select+patch", "verified-synth"})
# Aggregators that run code to choose (a patch against tests or checks). They run in benchmark
# mode, and verified-synth also under fusion_propose_patch, which hands the run a cross-checker
# that works on a sandbox copy (``PipelineContext.crosscheck``); no other tool runs code.
CODE_AGGREGATORS = frozenset({"verified", "verified-synth"})


class BenchmarkOnlyError(ConfigError):
    """A strategy that runs code (``verified``, ``verified-synth``) was used where none may run."""


def may_run_code(strategy: Strategy, mode: Mode, *, crosscheck: bool) -> bool:
    """Whether a run may use ``strategy``: one that runs code needs benchmark mode, except
    ``verified-synth`` given a cross-checker (``crosscheck``: fusion_propose_patch's sandbox)."""
    if not strategy.runs_code or mode is Mode.BENCHMARK:
        return True
    return strategy.aggregator == "verified-synth" and crosscheck


class Mode(StrEnum):
    """How a run is used: serving Claude Code (real) or measured in a study (benchmark)."""

    REAL = "real"
    BENCHMARK = "benchmark"


@dataclass(frozen=True)
class ModeSettings:
    """What a mode changes about a run. The full stored ledger applies to both modes."""

    lifetime_stats: bool  # append the lifetime-stats footer to display output
    shadow: bool  # shadow A/B runs may happen
    truncate_prompts: bool  # trim prompts that exceed a model's context window
    temperature: float | None  # sampling temperature for calls that do not choose one
    seed: int | None  # sampling seed for providers that accept one
    redact: bool  # scrub secret-looking text from the task before any model sees it
    stream: bool  # stream every call so time-to-first-token and decode speed are measured


MODE_SETTINGS: dict[Mode, ModeSettings] = {
    Mode.REAL: ModeSettings(
        lifetime_stats=True,
        shadow=True,
        truncate_prompts=True,
        temperature=None,
        seed=None,
        redact=True,
        stream=False,
    ),
    # Redaction is off because ground-truth tasks may contain secret-looking strings on purpose
    # (a review task about a hard-coded key); a study turns it back on with ``redact=True``.
    Mode.BENCHMARK: ModeSettings(
        lifetime_stats=False,
        shadow=False,
        truncate_prompts=False,
        temperature=0.0,
        seed=0,
        redact=False,
        stream=True,
    ),
}


class PanelMember(BaseModel):
    """One model in a strategy, with the knobs that may differ per member."""

    model_config = ConfigDict(extra="forbid")

    model: str  # catalog alias
    role: str = "auto"  # "auto": the catalog persona, else the task's own prompt; or a role name
    reasoning_effort: ReasoningEffort | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    # The member's lens (config/lenses.yaml) when the task type has it; None: one is given out.
    # A merged lens is written a+b. Checked against the configured lenses when strategies load.
    lens: str | None = None
    # The member's approach (config/approaches.yaml), for a select strategy; None: one is given out.
    approach: str | None = None

    @model_validator(mode="after")
    def _known_role(self) -> PanelMember:
        from fusion.orchestration.prompts import role_names

        if self.role != "auto" and self.role not in role_names():
            msg = f"unknown role '{self.role}'; use 'auto' or one of {sorted(role_names())}"
            raise ValueError(msg)
        return self


class CascadeSpec(BaseModel):
    """When a cascade stops after its cheap first wave and when it asks the rest of the panel.

    The ``first`` cheapest members answer first. If at least two answered, their agreement is at
    least ``agreement_threshold``, no point is disputed and the task is not high risk, the cascade
    returns at once with ``early_aggregator`` (no model call). Otherwise it escalates: the other
    members answer, then the strategy's ``rounds`` and ``aggregator`` apply as in a panel.
    """

    model_config = ConfigDict(extra="forbid")

    first: int = Field(default=2, ge=2)  # agreement needs two answers, so never fewer
    # Provisional default; the benchmark study tunes it (roadmap task 19).
    agreement_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    # On a run with lenses, vote and best_of become union (they would drop each lens's findings).
    early_aggregator: Literal["vote", "best_of", "union"] = "vote"
    escalate_on_high_risk: bool = True


class FanoutOverride(BaseModel):
    """Latency controls of the panel fan-out that one strategy sets for itself, on top of the
    routing policy's ``fanout`` section (only the fields set here change; ``off`` switches a
    control off, so a benchmark arm can measure the panel without it)."""

    model_config = ConfigDict(extra="forbid")

    # stop waiting for stragglers shortly after quorum
    early_return: EarlyReturn | Literal["off"] | None = None
    # ask another model when one is slow
    hedge_after_ms: Annotated[float, Field(gt=0)] | Literal["off"] | None = None

    @field_validator("early_return", "hedge_after_ms", mode="before")
    @classmethod
    def _false_is_off(cls, value: Any) -> Any:
        return "off" if value is False else value  # YAML reads `off` and `false` as False

    def applied_to(self, config: FanoutConfig) -> FanoutConfig:
        """``config`` with the fields this override sets (``off`` switches a control off)."""
        updates = {
            name: None if value == "off" else value
            for name in self.model_fields_set
            if (value := getattr(self, name)) is not None
        }
        return config.model_copy(update=updates) if updates else config


class SkipAggregator(BaseModel):
    """Skip an ``llm`` aggregator when the panel already agrees.

    When at least two members answered, their agreement is at least ``agreement_threshold``, no
    point is disputed and the task is not high risk, the final answer is built by ``use`` (no
    model call) instead of the synthesizer, the same test a cascade applies to its first wave.
    """

    model_config = ConfigDict(extra="forbid")

    agreement_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    # On a run with lenses, vote and best_of become union (they would drop each lens's findings).
    use: Literal["vote", "best_of", "union"] = "vote"
    escalate_on_high_risk: bool = True


class EscalationOverride(BaseModel):
    """What one ``kind: auto`` strategy changes about the ladder (``routing.escalation``)."""

    model_config = ConfigDict(extra="forbid")

    allow_frontier: bool | None = None
    max_rungs: int | None = Field(default=None, ge=1)

    def applied_to(self, policy: EscalationPolicy) -> EscalationPolicy:
        updates = {
            name: value
            for name in self.model_fields_set
            if (value := getattr(self, name)) is not None
        }
        return policy.model_copy(update=updates) if updates else policy


class Strategy(BaseModel):
    """A declarative arm: members, rounds, aggregation, judging and caps.

    ``kind: auto`` is the exception: it has no members of its own. It estimates the request's
    difficulty and climbs the ladder of ``routing.escalation``, whose rungs name the strategies
    that answer (see ``fusion.orchestration.escalate``).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["solo", "panel", "cascade", "auto"]
    description: str = ""
    members: list[PanelMember] = Field(default_factory=list)  # solo = exactly one member
    rounds: int = Field(default=1, ge=1, le=4)  # > 1 adds peer-refinement rounds
    # auto (the default): per task, union for review and debugging (answers add together) and
    # select+patch for plans, decisions, asks and patches (answers are rivals).
    aggregator: AggregatorName = "auto"
    # The model of an llm, union+check or select aggregator; None = the catalog's synthesizer role.
    aggregator_model: str | None = None
    judge: Literal["off", "light", "full"] = "off"
    # The synthesizer reads the judge's scores, so synthesis waits for the judge. Otherwise the
    # two run at the same time.
    judge_feeds_synthesis: bool = False
    max_cost_usd: float | None = Field(default=None, gt=0)
    max_latency_s: float | None = Field(default=None, gt=0)
    cascade: CascadeSpec | None = None
    fanout: FanoutOverride | None = None  # this strategy's latency controls (see FanoutOverride)
    skip_aggregator: SkipAggregator | None = None  # no synthesis call when the panel agrees
    # fast: members ranked by measured speed (catalog observed_*), so a cascade's first wave and a
    # panel cut by max_models are the fastest models; balanced: the configured order (a cascade
    # still goes cheapest first).
    speed: Literal["fast", "balanced"] = "balanced"
    # auto: members of a task type that has lenses (config/lenses.yaml) each cover one area;
    # off: every member answers the whole task. Unset: auto for a panel whose aggregator keeps
    # each lens's findings (LENS_AGGREGATORS), off otherwise.
    lenses: Literal["auto", "off"] | None = None
    # auto: with a select aggregator each member answers the whole task from its own approach
    # (config/approaches.yaml), so the rival answers differ; off: identical prompts. Unset: auto
    # for a panel with a select aggregator, off otherwise.
    approaches: Literal["auto", "off"] | None = None
    # Ask each member to check its answer against the input and report an honest confidence;
    # the ladder escalates a single model whose confidence is low (rung 0's "short self-check").
    self_check: bool = False
    # kind auto only: what this strategy changes about the ladder (e.g. allow_frontier: true).
    escalation: EscalationOverride | None = None

    @field_validator("lenses", "approaches", mode="before")
    @classmethod
    def _bool_switch(cls, value: Any) -> Any:
        if isinstance(value, bool):  # YAML reads `off` as False and `on` as True
            return "auto" if value else "off"
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Strategy:
        if self.kind == "auto":
            return self._consistent_auto()
        problems: list[str] = []
        if not self.members:
            problems.append(f"a {self.kind} strategy needs at least one member")
        if self.escalation is not None:
            problems.append("'escalation' is only valid for kind 'auto'")
        selecting = self.aggregator in SELECT_AGGREGATORS
        if self.lenses is None:
            lensable = self.kind == "panel" and self.aggregator in LENS_AGGREGATORS
            self.lenses = "auto" if lensable else "off"
        elif self.lenses == "auto":
            if self.kind == "solo":
                problems.append("a solo strategy has one member, so it takes no lenses")
            if self.kind == "cascade":
                problems.append(
                    "a cascade stops when its first wave agrees, and lensed answers cover "
                    "different areas instead of agreeing; set lenses: off"
                )
            if selecting:
                problems.append(
                    f"a {self.aggregator} aggregator picks ONE whole answer among rivals, and "
                    "lenses make each answer cover only one area, so there would be no whole "
                    "answer to pick; its members get approaches instead (set lenses: off, or use "
                    "aggregator: auto, which lenses review and debugging and selects for the rest)"
                )
            elif self.aggregator not in LENS_AGGREGATORS:
                problems.append(
                    f"lenses split the task by area, and a {self.aggregator} aggregator keeps "
                    "only what several answers share (or one whole answer); use union, "
                    "union+check, llm or digest, or set lenses: off"
                )
        if self.approaches is None:
            self.approaches = "auto" if self.kind == "panel" and selecting else "off"
        elif self.approaches == "auto" and not selecting:
            problems.append(
                "approaches give each member a stance for a select aggregator (select, "
                f"select+patch); a {self.aggregator} aggregator does not pick among rivals"
            )
        if any(m.lens for m in self.members) and selecting:
            problems.append(
                f"a member names a lens, but a {self.aggregator} aggregator picks ONE whole "
                "answer among rivals and lenses make each answer cover only one area"
            )
        if any(m.approach for m in self.members) and self.approaches != "auto":
            problems.append(
                "a member names an approach, which needs a select aggregator with "
                "approaches: auto"
            )
        if self.kind == "cascade":
            if self.cascade is None:
                self.cascade = CascadeSpec()
            if len(self.members) <= self.cascade.first:
                problems.append(
                    f"a cascade needs more than cascade.first ({self.cascade.first}) members "
                    "to have anyone to escalate to"
                )
        elif self.cascade is not None:
            problems.append("'cascade' is only valid for kind 'cascade'")
        aliases = [m.model for m in self.members]
        if len(set(aliases)) != len(aliases) and "auto" not in {self.lenses, self.approaches}:
            problems.append(
                "a model may appear more than once in members only with lenses or approaches "
                "(each copy then covers another area or answers from another stance); without "
                "them the copies would give the same answer"
            )
        named = [(m.model, m.lens) for m in self.members if m.lens]
        if len(set(named)) != len(named):
            problems.append("two copies of one model name the same lens")
        stances = [(m.model, m.approach) for m in self.members if m.approach]
        if len(set(stances)) != len(stances):
            problems.append("two copies of one model name the same approach")
        if self.kind == "solo":
            if len(self.members) != 1:
                problems.append("a solo strategy has exactly one member")
            if self.rounds != 1:
                problems.append("a solo strategy has rounds: 1")
            if self.aggregator_model is not None:
                problems.append("a solo strategy has no aggregator_model")
        if self.judge_feeds_synthesis and self.judge == "off":
            problems.append("judge_feeds_synthesis needs judge: light or full")
        if self.judge_feeds_synthesis and (self.kind == "solo" or self.aggregator != "llm"):
            problems.append("judge_feeds_synthesis needs an llm aggregator")
        if self.skip_aggregator is not None and (self.kind == "solo" or self.aggregator != "llm"):
            problems.append("skip_aggregator needs an llm aggregator to skip")
        skip = self.skip_aggregator
        if skip is not None and skip.use == "union" and self.lenses != "auto":
            problems.append("skip_aggregator.use: union needs lenses")
        if self.aggregator == "verified-synth" and self.rounds != 1:
            problems.append(
                "verified-synth takes rounds: 1: members write their patch and checks once, and a "
                "refinement round would rewrite answers without them"
            )
        if self.aggregator in NO_MODEL_AGGREGATORS and self.aggregator_model is not None:
            problems.append(
                f"a {self.aggregator} aggregator calls no model, so aggregator_model must be unset"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _consistent_auto(self) -> Strategy:
        """kind auto: the rungs' strategies answer, so everything about answering is theirs."""
        problems: list[str] = []
        if self.members:
            problems.append(
                "an auto strategy takes no members: the strategies named by the rungs of "
                "routing.escalation answer"
            )
        theirs = {
            "rounds": self.rounds != 1,
            "aggregator": self.aggregator != "auto",
            "aggregator_model": self.aggregator_model is not None,
            "judge": self.judge != "off",
            "judge_feeds_synthesis": self.judge_feeds_synthesis,
            "cascade": self.cascade is not None,
            "fanout": self.fanout is not None,
            "skip_aggregator": self.skip_aggregator is not None,
            "speed": self.speed != "balanced",
            "lenses": self.lenses == "auto",
            "approaches": self.approaches == "auto",
            "self_check": self.self_check,
        }
        named = [field for field, set_here in theirs.items() if set_here]
        if named:
            problems.append(
                f"an auto strategy leaves {', '.join(named)} to the strategies of its rungs"
            )
        if problems:
            raise ValueError("; ".join(problems))
        self.lenses = self.approaches = "off"
        return self

    @property
    def runs_code(self) -> bool:
        """Whether its aggregator runs code to choose (``CODE_AGGREGATORS``): benchmark only."""
        return self.aggregator in CODE_AGGREGATORS

    @property
    def calls_aggregator_model(self) -> bool:
        """Whether aggregation makes a model call (llm or union+check, not for a solo run)."""
        return self.kind not in ("solo", "auto") and self.aggregator in MODEL_AGGREGATORS

    @property
    def models(self) -> list[str]:
        """Every catalog alias the strategy can call (members and aggregator)."""
        extra = [self.aggregator_model] if self.aggregator_model else []
        return [m.model for m in self.members] + extra


def member_overrides(member: PanelMember | None) -> dict[str, Any]:
    """ModelRequest fields a member sets; unset fields keep the catalog and mode defaults."""
    if member is None:
        return {}
    overrides: dict[str, Any] = {}
    if member.temperature is not None:
        overrides["temperature"] = member.temperature
    if member.reasoning_effort is not None:
        overrides["reasoning_effort"] = member.reasoning_effort
    return overrides


class StrategyBook:
    """The configured strategies, the budget-to-strategy table and old names of strategies."""

    def __init__(
        self,
        strategies: Mapping[str, Strategy],
        budget_map: Mapping[str, str],
        aliases: Mapping[str, str] | None = None,
        lenses: LensBook | None = None,
        approaches: ApproachBook | None = None,
    ) -> None:
        self.strategies = dict(strategies)
        self._lenses = lenses
        self._approaches = approaches
        self.budget_map = dict(budget_map)
        # An alias is a strategy's previous name, kept working for one release.
        self.aliases = {
            old: new for old, new in (aliases or {}).items() if old not in self.strategies
        }
        known = sorted(self.strategies)
        for old, new in self.aliases.items():
            if new not in self.strategies:
                msg = (
                    f"strategy_aliases.{old} names unknown strategy '{new}'; "
                    f"defined strategies: {', '.join(known)}"
                )
                raise ConfigError(msg)
        for budget, name in self.budget_map.items():
            if name not in self.strategies and name not in self.aliases:
                msg = (
                    f"budget_strategies.{budget} names unknown strategy '{name}'; "
                    f"defined strategies: {', '.join(known)}"
                )
                raise ConfigError(msg)

    def names(self) -> list[str]:
        return sorted(self.strategies)

    @property
    def lenses(self) -> LensBook:
        """The configured lenses (loaded on first use when the book was built without them)."""
        if self._lenses is None:
            from fusion.orchestration.lenses import load_lens_book

            self._lenses = load_lens_book()
        return self._lenses

    @property
    def approaches(self) -> ApproachBook:
        """The configured approaches (loaded on first use when the book was built without them)."""
        if self._approaches is None:
            from fusion.orchestration.approaches import load_approach_book

            self._approaches = load_approach_book()
        return self._approaches

    def get(self, name: str) -> Strategy:
        if name in self.strategies:
            return self.strategies[name]
        if name in self.aliases:
            return self.strategies[self.aliases[name]]
        close = difflib.get_close_matches(name, [*self.names(), *self.aliases], n=1)
        suggestion = self.aliases.get(close[0], close[0]) if close else None
        hint = f"; did you mean '{suggestion}'?" if suggestion else ""
        msg = f"Unknown strategy '{name}'{hint} Defined strategies: {', '.join(self.names())}"
        raise ConfigError(msg)

    def for_budget(self, budget: BudgetLevel | str) -> Strategy:
        key = budget.value if isinstance(budget, BudgetLevel) else str(budget)
        if key not in self.budget_map:
            msg = f"No strategy is mapped to budget '{key}' in budget_strategies"
            raise ConfigError(msg)
        return self.get(self.budget_map[key])

    def resolve(self, name: str | None, budget: BudgetLevel | str) -> Strategy:
        """An explicit strategy wins; otherwise the budget's strategy."""
        return self.get(name) if name else self.for_budget(budget)

    def referenced_models(self) -> set[str]:
        return {alias for strategy in self.strategies.values() for alias in strategy.models}


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


class _StrategyConfig(BaseModel):
    strategies: dict[str, Strategy]
    budget_strategies: dict[str, str] = Field(default_factory=dict)
    strategy_aliases: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _name_from_key(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("strategies"), dict):
            strategies = dict(data["strategies"])
            # A user override written under a strategy's old name ("panel-cheap: {rounds: 2}",
            # no kind of its own) is applied to the renamed strategy.
            for old, new in (data.get("strategy_aliases") or {}).items():
                partial = strategies.get(old)
                target = strategies.get(new)
                if isinstance(partial, dict) and "kind" not in partial and isinstance(target, dict):
                    strategies[new] = _merge(target, partial)
                    del strategies[old]
            named = {
                key: {**value, "name": key} if isinstance(value, dict) else value
                for key, value in strategies.items()
            }
            return {**data, "strategies": named}
        return data


def load_strategy_book(path: Path | None = None) -> StrategyBook:
    """Load strategies from the layered configuration, or from one explicit YAML file."""
    resolved = None
    if path is not None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            msg = f"Expected a mapping in {path}"
            raise ConfigError(msg)
    else:
        resolved = resolve_config()
        raw = {k: resolved.data[k] for k in _STRATEGY_KEYS if k in resolved.data}
    try:
        config = _StrategyConfig.model_validate(raw)
    except ValidationError as exc:
        raise format_validation_error(exc, section_prefix="", resolved=resolved) from exc
    book = StrategyBook(config.strategies, config.budget_strategies, config.strategy_aliases)
    _check_lenses(book)
    _check_approaches(book)
    return book


def _check_lenses(book: StrategyBook) -> None:
    """Every lens a member names is a configured lens."""
    for name, strategy in book.strategies.items():
        for member in strategy.members:
            if member.lens is None:
                continue
            try:
                book.lenses.get(member.lens)
            except ConfigError as exc:
                msg = f"strategies.{name}: member {member.model}: {exc}"
                raise ConfigError(msg) from exc


def _check_approaches(book: StrategyBook) -> None:
    """Every approach a member names is a configured approach."""
    for name, strategy in book.strategies.items():
        for member in strategy.members:
            if member.approach is None:
                continue
            try:
                book.approaches.get(member.approach)
            except ConfigError as exc:
                msg = f"strategies.{name}: member {member.model}: {exc}"
                raise ConfigError(msg) from exc


def mock_strategy_book(book: StrategyBook, registry: ModelRegistry) -> StrategyBook:
    """Keep every strategy's shape but run it on the registry's mock models (offline mode).

    Offline mode is a different configuration, not a different code path: each model becomes the
    next mock panel model (a model listed twice becomes the same mock twice, so self-mixtures stay
    self-mixtures), and an llm aggregator becomes the mock synthesizer.
    """
    panel = registry.by_role("panel")
    synthesizer = (registry.by_role("synthesizer") or registry.by_role("judge") or panel)[0]
    mapped: dict[str, Strategy] = {}
    for name, strategy in book.strategies.items():
        distinct = list(dict.fromkeys(m.model for m in strategy.members))
        mock_of = dict(zip(distinct, panel, strict=False))
        members = [
            member.model_copy(update={"model": mock_of[member.model]})
            for member in strategy.members
            if member.model in mock_of
        ]
        mapped[name] = strategy.model_copy(
            update={
                "members": members,
                "aggregator_model": synthesizer if strategy.aggregator_model else None,
            }
        )
    return StrategyBook(mapped, book.budget_map, book.aliases, book.lenses, book.approaches)
