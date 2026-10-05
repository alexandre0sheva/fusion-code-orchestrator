"""The optional identical-request response cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from _scripted import FAST, PROMPT, Scripted, context, pipeline, stages
from fusion.config.layers import ConfigError
from fusion.config.loader import CacheConfig, load_routing_policies
from fusion.orchestration.cache import ResponseCache, request_key
from fusion.orchestration.strategy import Mode, load_strategy_book


@pytest.fixture
def cache_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION__CACHE__ENABLED", "true")


def test_the_cache_is_off_by_default() -> None:
    config = load_routing_policies().cache
    assert (config.enabled, config.ttl_seconds, config.max_entries) == (False, 900.0, 128)


def test_cache_settings_are_validated() -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        CacheConfig(ttl_seconds=0)
    with pytest.raises(ValueError, match="max_entries"):
        CacheConfig(max_entries=0)


def test_a_misspelled_cache_section_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION__CASHE__ENABLED", "true")
    with pytest.raises(ConfigError, match="cache"):
        load_routing_policies()


# ----------------------------------------------------------------------------------------- keys


def _key(prompt: str = PROMPT, strategy: str = "panel-cheap", **kw: object) -> str:
    return request_key(context(prompt, **kw), load_strategy_book().get(strategy))  # type: ignore[arg-type]


def test_keys_ignore_secrets_because_models_never_see_them() -> None:
    one = _key(PROMPT + " token sk-ant-api03-" + "A" * 40)
    two = _key(PROMPT + " token sk-ant-api03-" + "B" * 40)
    assert one == two != _key(PROMPT)


def test_keys_change_with_anything_that_changes_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _key()
    assert _key("another question") != base
    assert _key(strategy="panel-digest") != base
    assert _key(changed_files=["a.py"]) != base
    assert _key(max_models=2) != base
    assert _key(context="extra") != base
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CHEAP__AGGREGATOR_MODEL", "claude-sonnet")
    assert _key() != base  # editing the strategy never serves the old strategy's answer


def test_keys_do_not_depend_on_shadow_settings() -> None:
    assert _key(shadow_baseline=True) == _key(shadow_baseline=False)


# ---------------------------------------------------------------------- the store (unit, clock)


async def _result(tmp_path: Path) -> object:
    return await pipeline(tmp_path, Scripted()).run(context())


async def test_entries_expire_after_their_ttl_and_the_least_recent_is_evicted(
    tmp_path: Path,
) -> None:
    now = [0.0]
    cache = ResponseCache(CacheConfig(enabled=True, ttl_seconds=10, max_entries=2), lambda: now[0])
    result = await _result(tmp_path)
    cache.put("a", result)  # type: ignore[arg-type]
    cache.put("b", result)  # type: ignore[arg-type]
    assert cache.get("a") is not None  # touching a makes b the least recent
    cache.put("c", result)  # type: ignore[arg-type]
    assert cache.get("b") is None and cache.get("a") is not None and cache.get("c") is not None
    now[0] = 10.5
    assert cache.get("a") is None  # older than the ttl
    assert (cache.hits, cache.misses) == (3, 2)


# ------------------------------------------------------------------------------ in the pipeline


async def test_an_identical_request_is_answered_without_calling_a_model(
    tmp_path: Path, cache_on: None
) -> None:
    provider = Scripted()
    pipe = pipeline(tmp_path, provider)
    first = await pipe.run(context())
    calls = len(provider.requests)
    assert calls > 0 and not first.cache_hit
    second = await pipe.run(context())
    assert len(provider.requests) == calls  # nothing new was asked
    assert second.cache_hit and second.final_answer == first.final_answer
    assert second.total_cost_usd == 0.0 and second.ledger is not None
    assert second.ledger.records == []
    assert second.usage is not None and second.usage.successful_model_calls == 0
    assert any("Served from the response cache" in w for w in second.warnings)
    assert not any("response cache" in w for w in first.warnings)  # the stored run is untouched
    assert second.cost_comparison is not None
    assert second.cost_comparison.fusion_total_cost_usd == 0.0


async def test_a_hit_is_reported_in_the_output_the_caller_sees(
    tmp_path: Path, cache_on: None
) -> None:
    pipe = pipeline(tmp_path, Scripted())
    await pipe.run(context())
    hit = await pipe.run(context())
    presenter = pipe.deps.presenter
    usage = presenter.usage_for(hit)
    text = presenter.display_markdown(
        title="t",
        result=hit,
        usage=usage,
        cost_comparison=presenter.comparison_for(hit, usage),
        detail="full",
    )
    assert "Served from the response cache" in text and "Fusion cost: $0.0000" in text


async def test_a_different_strategy_or_prompt_is_a_miss(tmp_path: Path, cache_on: None) -> None:
    provider = Scripted()
    pipe = pipeline(tmp_path, provider)
    await pipe.run(context())
    calls = len(provider.requests)
    other_strategy = await pipe.run(context(strategy="panel-digest"))
    other_prompt = await pipe.run(context("What is a context manager in Python?"))
    assert not other_strategy.cache_hit and not other_prompt.cache_hit
    assert len(provider.requests) > calls


async def test_nothing_is_cached_unless_enabled(tmp_path: Path) -> None:
    provider = Scripted()
    pipe = pipeline(tmp_path, provider)
    await pipe.run(context())
    calls = len(provider.requests)
    again = await pipe.run(context())
    assert not again.cache_hit and len(provider.requests) == 2 * calls


async def test_benchmark_mode_neither_reads_nor_writes_the_cache(
    tmp_path: Path, cache_on: None
) -> None:
    provider = Scripted()
    pipe = pipeline(tmp_path, provider)
    await pipe.run(context(), mode=Mode.BENCHMARK)  # must not store
    after_benchmark = len(provider.requests)
    real = await pipe.run(context())  # so this is a miss
    assert not real.cache_hit and len(provider.requests) == 2 * after_benchmark
    benchmark = await pipe.run(context(), mode=Mode.BENCHMARK)  # and a stored entry is not read
    assert not benchmark.cache_hit and len(provider.requests) == 3 * after_benchmark


async def test_a_halted_run_is_not_cached(tmp_path: Path, cache_on: None) -> None:
    provider = Scripted(fail={FAST, "mock-security", "mock-weak"})
    pipe = pipeline(tmp_path, provider)
    first = await pipe.run(context())
    assert "quorum was not met" in first.final_answer
    calls = len(provider.requests)
    again = await pipe.run(context())
    assert not again.cache_hit and len(provider.requests) > calls


async def test_a_cached_cascade_keeps_its_outcome(tmp_path: Path, cache_on: None) -> None:
    pipe = pipeline(tmp_path, Scripted())
    first = await pipe.run(context(strategy="panel-cascade"))
    hit = await pipe.run(context(strategy="panel-cascade"))
    assert hit.cache_hit and hit.cascade == first.cascade
    assert stages(first) and stages(hit) == []
