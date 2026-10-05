"""Arms: the contestants of a study, resolved to strategies.

An arm is a strategy by name, optionally with overrides, so "the cheap panel with two refinement
rounds" is one line of configuration and runs through the same pipeline as every other arm.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from fusion.bench.spec import Arm
from fusion.config.layers import ConfigError
from fusion.orchestration.strategy import Strategy, StrategyBook

__all__ = ["DEFAULT_ARMS", "arm_book", "parse_arms", "resolve_arm"]

# The six arms of the roadmap's study: frontier solo, cheap solo, cheap panel, the panel with
# refinement, the cascade, and the panel with a strong synthesizer.
DEFAULT_ARMS = (
    "solo-frontier",
    "solo-cheap",
    "panel-cheap",
    "panel-refine",
    "panel-cascade",
    "panel-cheap-strong-synth",
)


def parse_arms(text: str) -> list[Arm]:
    """Arms from ``a,b,name=strategy``; the word ``default`` stands for the six default arms."""
    arms: list[Arm] = []
    for token in (t.strip() for t in text.split(",")):
        if not token:
            continue
        if token == "default":
            arms.extend(Arm(name=n, strategy=n) for n in DEFAULT_ARMS)
            continue
        name, sep, strategy = token.partition("=")
        arms.append(Arm(name=name.strip(), strategy=(strategy if sep else name).strip()))
    if not arms:
        msg = "--arms needs at least one strategy name (or 'default' for the six standard arms)"
        raise ConfigError(msg)
    return arms


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def resolve_arm(arm: Arm, book: StrategyBook) -> Strategy:
    """The arm's strategy with its overrides applied, named after the arm."""
    base = book.get(arm.strategy)
    data = _merge(base.model_dump(mode="python"), arm.overrides)
    data["name"] = arm.name
    try:
        return Strategy.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        msg = f"Arm '{arm.name}' (strategy '{arm.strategy}') is not valid: {problems}"
        raise ConfigError(msg) from exc


def arm_book(book: StrategyBook, arms: list[Arm]) -> StrategyBook:
    """The configured strategies plus one per arm, so the pipeline can run an arm by its name."""
    strategies = dict(book.strategies)
    for arm in arms:
        strategies[arm.name] = resolve_arm(arm, book)
    return StrategyBook(strategies, book.budget_map)
