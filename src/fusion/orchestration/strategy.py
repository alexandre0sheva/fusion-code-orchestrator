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
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from fusion.config.layers import ConfigError, format_validation_error, resolve_config
from fusion.config.loader import EarlyReturn
from fusion.routing.budget import BudgetLevel

if TYPE_CHECKING:
    from fusion.routing.model_registry import ModelRegistry

__all__ = [
    "MODE_SETTINGS",
    "BenchmarkOnlyError",
    "CascadeSpec",
    "FanoutOverride",
    "Mode",
    "ModeSettings",
    "PanelMember",
    "Strategy",
    "StrategyBook",
    "load_strategy_book",
    "member_overrides",
    "mock_strategy_book",
]

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
_STRATEGY_KEYS = ("strategies", "budget_strategies")
# Aggregators that make no model call, so they take no aggregator_model.
NO_MODEL_AGGREGATORS = frozenset({"vote", "best_of", "digest", "verified"})


class BenchmarkOnlyError(ConfigError):
    """A strategy that runs code (the ``verified`` aggregator) was used outside a benchmark."""


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
    early_aggregator: Literal["vote", "best_of"] = "vote"
    escalate_on_high_risk: bool = True


class FanoutOverride(BaseModel):
    """Latency controls of the panel fan-out that one strategy sets for itself, on top of the
    routing policy's ``fanout`` section (only the fields set here change)."""

    model_config = ConfigDict(extra="forbid")

    early_return: EarlyReturn | None = None  # stop waiting for stragglers shortly after quorum
    hedge_after_ms: float | None = Field(default=None, gt=0)  # ask another model when one is slow


class Strategy(BaseModel):
    """A declarative arm: members, rounds, aggregation, judging and caps."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["solo", "panel", "cascade"]
    description: str = ""
    members: list[PanelMember] = Field(min_length=1)  # solo = exactly one member
    rounds: int = Field(default=1, ge=1, le=4)  # > 1 adds peer-refinement rounds
    aggregator: Literal["llm", "vote", "best_of", "digest", "verified"] = "llm"
    aggregator_model: str | None = None  # llm aggregator; None = the catalog's synthesizer role
    judge: Literal["off", "light", "full"] = "off"
    # The synthesizer reads the judge's scores, so synthesis waits for the judge. Otherwise the
    # two run at the same time.
    judge_feeds_synthesis: bool = False
    max_cost_usd: float | None = Field(default=None, gt=0)
    max_latency_s: float | None = Field(default=None, gt=0)
    cascade: CascadeSpec | None = None
    fanout: FanoutOverride | None = None  # this strategy's latency controls (see FanoutOverride)

    @model_validator(mode="after")
    def _consistent(self) -> Strategy:
        problems: list[str] = []
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
        if len(set(aliases)) != len(aliases):
            problems.append("a model may appear only once in members")
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
        if self.aggregator in NO_MODEL_AGGREGATORS and self.aggregator_model is not None:
            problems.append(
                f"a {self.aggregator} aggregator calls no model, so aggregator_model must be unset"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self

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
    """The configured strategies and the budget-to-strategy table."""

    def __init__(self, strategies: Mapping[str, Strategy], budget_map: Mapping[str, str]) -> None:
        self.strategies = dict(strategies)
        self.budget_map = dict(budget_map)
        known = sorted(self.strategies)
        for budget, name in self.budget_map.items():
            if name not in self.strategies:
                msg = (
                    f"budget_strategies.{budget} names unknown strategy '{name}'; "
                    f"defined strategies: {', '.join(known)}"
                )
                raise ConfigError(msg)

    def names(self) -> list[str]:
        return sorted(self.strategies)

    def get(self, name: str) -> Strategy:
        if name in self.strategies:
            return self.strategies[name]
        close = difflib.get_close_matches(name, self.names(), n=1)
        hint = f"; did you mean '{close[0]}'?" if close else ""
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


class _StrategyConfig(BaseModel):
    strategies: dict[str, Strategy]
    budget_strategies: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _name_from_key(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("strategies"), dict):
            named = {
                key: {**value, "name": key} if isinstance(value, dict) else value
                for key, value in data["strategies"].items()
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
    return StrategyBook(config.strategies, config.budget_strategies)


def mock_strategy_book(book: StrategyBook, registry: ModelRegistry) -> StrategyBook:
    """Keep every strategy's shape but run it on the registry's mock models (offline mode).

    Offline mode is a different configuration, not a different code path: each member becomes the
    next mock panel model, and an llm aggregator becomes the mock synthesizer.
    """
    panel = registry.by_role("panel")
    synthesizer = (registry.by_role("synthesizer") or registry.by_role("judge") or panel)[0]
    mapped: dict[str, Strategy] = {}
    for name, strategy in book.strategies.items():
        members = [
            member.model_copy(update={"model": alias})
            for member, alias in zip(strategy.members, panel, strict=False)
        ]
        mapped[name] = strategy.model_copy(
            update={
                "members": members,
                "aggregator_model": synthesizer if strategy.aggregator_model else None,
            }
        )
    return StrategyBook(mapped, book.budget_map)
