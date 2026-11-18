"""Neutral judges: a study with ``neutral_judges`` refuses a judge whose vendor serves an arm."""

from __future__ import annotations

from pathlib import Path

import pytest

from _bench import config, sim_env, write_dataset
from fusion.bench.arms import arm_book
from fusion.bench.judges import arm_aliases, check_neutral_judges, judge_conflicts
from fusion.bench.runner import run_bench
from fusion.config.layers import ConfigError
from fusion.routing.policy import RoutingPolicy


def _setup(tmp_path: Path, arms: str, **kw: object):  # type: ignore[no-untyped-def]
    cfg = config(write_dataset(tmp_path / "d.jsonl"), arms, repeats=1, **kw)
    env = sim_env(cfg, tmp_path)
    book = arm_book(env.book, cfg.arms)
    routing = RoutingPolicy(env.routing_config, registry=env.registry, strategies=book)
    return cfg, env, {a.name: book.get(a.name) for a in cfg.arms}, routing


def test_an_arm_uses_every_model_on_every_rung_it_may_climb(tmp_path: Path) -> None:
    _, env, strategies, routing = _setup(tmp_path, "panel-open,auto-frontier")
    assert set(arm_aliases(strategies["panel-open"], routing)) == {
        "deepseek-flash",
        "minimax-flash",
        "qwen-flash",
        "glm",
        "claude-haiku",  # the aggregator
    }
    climbed = arm_aliases(strategies["auto-frontier"], routing)
    assert "claude-opus" in climbed and "claude-sonnet" in climbed  # the mid and frontier rungs


def test_a_judge_from_a_vendor_on_any_arm_is_a_conflict(tmp_path: Path) -> None:
    _, env, strategies, routing = _setup(tmp_path, "panel-open,panel-nano")
    models = env.registry.models
    found = judge_conflicts(["mimo-flash", "qwen-flash", "gpt-sol"], strategies, routing, models)
    assert found == [
        "qwen-flash (qwen) shares its vendor with panel-open",
        "gpt-sol (openai) shares its vendor with panel-nano",
    ]
    check_neutral_judges(["mimo-flash", "hy4-flash"], strategies, routing, models)  # no raise
    with pytest.raises(ConfigError, match="names no judge_models"):
        check_neutral_judges([], strategies, routing, models)


async def test_a_study_with_neutral_judges_refuses_a_conflicted_judge(tmp_path: Path) -> None:
    cfg, env, _, _ = _setup(
        tmp_path, "panel-nano", judge_models=["claude-sonnet"], neutral_judges=True
    )
    with pytest.raises(ConfigError, match="claude-sonnet \\(anthropic\\)"):
        await run_bench(cfg, env=env)
    ok = cfg.model_copy(update={"judge_models": ["mimo-flash"]})
    run = await run_bench(ok, env=env)
    assert run.items and all(i.status == "completed" for i in run.items)


def test_models_the_router_picks_at_run_time_count_as_the_arms(tmp_path: Path) -> None:
    """A selector or synthesizer the strategy does not name is the catalog's synthesizer, and an
    in-run judge is each task type's judge: both shape the answer, so both count."""
    _, env, strategies, routing = _setup(tmp_path, "panel-luna-approaches,panel-open")
    unnamed = strategies["panel-luna-approaches"].model_copy(update={"aggregator_model": None})
    assert "claude-haiku" in arm_aliases(unnamed, routing)  # the catalog's first synthesizer
    found = judge_conflicts(
        ["claude-sonnet"], {"luna-select": unnamed}, routing, env.registry.models
    )
    assert found == ["claude-sonnet (anthropic) shares its vendor with luna-select"]
    judged = strategies["panel-open"].model_copy(update={"judge": "light"})
    assert "gemini-flash" in arm_aliases(judged, routing)  # the task types' judge
    assert "gemini-flash" not in arm_aliases(strategies["panel-open"], routing)
