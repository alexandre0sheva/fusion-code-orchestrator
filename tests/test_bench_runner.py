"""Running a study: resume, caps, determinism, concurrency, the cache, and benchmark mode."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from _bench import config, point, sim_env, worst_job_usd, write_dataset
from fusion.bench.cache import CachingProvider, ResponseDiskCache
from fusion.bench.runner import BenchEnv, BenchProgress, BenchRun, run_bench
from fusion.bench.scoring import SCORERS, PointsScorer
from fusion.bench.spend import SpendLedger
from fusion.bench.store import BenchItem, BenchStore
from fusion.bench.virtual import run_virtual
from fusion.config.layers import ConfigError
from fusion.providers.base import ModelRequest, ModelResponse, close_providers


def _run(cfg: Any, env: BenchEnv, run_id: str = "r1", **kw: Any) -> BenchRun:
    return run_virtual(run_bench(cfg, env=env, run_id=run_id, **kw))


def _rounded(value: Any) -> Any:
    """Floats to microseconds: a job that starts later on the virtual clock subtracts larger
    numbers, so its timings agree only to the last few bits."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    return value


def _stable(run: BenchRun) -> list[dict[str, Any]]:
    """Items as a comparison sees them: everything but the run's name, in a fixed order."""
    rows = [_rounded(i.model_dump(mode="json", exclude={"run_id"})) for i in run.items]
    return sorted(rows, key=lambda r: r["job_key"])


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    return write_dataset(tmp_path / "data.jsonl", count=4)


# ------------------------------------------------------------------------------ end to end


def test_a_mock_run_finishes_on_the_toy_dataset_with_every_measurement(tmp_path: Path) -> None:
    cfg = config(Path("toy"), arms="default", repeats=2)
    cfg = cfg.model_copy(
        update={"dataset": Path(__file__).parents[1] / "src/fusion/bench/datasets/toy.jsonl"}
    )
    run = _run(cfg, sim_env(cfg, tmp_path))
    assert run.status == "completed" and run.total_jobs == run.done_jobs == 8 * 6 * 2
    assert run.failed_jobs == 0 and run.stop_reason is None
    assert {i.arm for i in run.items} == set(a.name for a in cfg.arms)
    for item in run.items:
        m = item.metrics
        assert item.status == "completed" and item.score is not None
        assert m.seconds_to_complete > 0 and m.cost_usd > 0 and m.calls >= 1
        assert m.input_tokens > 0 and m.output_tokens > 0 and m.output_tokens_per_s
        assert m.decode_tokens_per_s and m.ttft_ms  # streaming was on, so these are measured
        assert m.quality == item.score.quality and m.solved == (m.quality >= 0.6)
        assert m.eval_cost_usd == 0 and m.cache_hits == 0 and m.latency_valid
        assert len(item.calls) == m.calls  # the full ledger is stored with the item


def test_the_stored_run_can_be_read_back_from_disk_and_sqlite(
    tmp_path: Path, dataset: Path
) -> None:
    cfg = config(dataset)
    env = sim_env(cfg, tmp_path)
    run = _run(cfg, env)
    store = BenchStore(tmp_path)
    assert (tmp_path / "r1" / "results.jsonl").read_text().count("\n") == run.total_jobs
    assert json.loads((tmp_path / "r1" / "config.json").read_text())["repeats"] == 2
    record = store.get_run("r1")
    assert record and record.status == "completed" and record.done_jobs == run.total_jobs
    assert record.spent_usd == pytest.approx(run.spent_usd)
    assert len(store.items("r1")) == run.total_jobs
    assert store._db().execute("SELECT COUNT(*) FROM bench_items").fetchone()[0] == run.total_jobs


def test_a_simulated_run_repeats_exactly(tmp_path: Path, dataset: Path) -> None:
    cfg = config(dataset, arms="solo-frontier,panel-cheap,panel-cascade")
    first = _run(cfg, sim_env(cfg, tmp_path / "a"), "r1")
    second = _run(cfg, sim_env(cfg, tmp_path / "b"), "r2")
    assert _stable(first) == _stable(second)  # answers, costs, tokens and timings, bit for bit
    assert any(i.metrics.seconds_to_complete > 1 for i in first.items)


def test_repeats_of_a_job_differ_but_each_is_reproducible(tmp_path: Path, dataset: Path) -> None:
    cfg = config(dataset, arms="solo-cheap", repeats=3)
    run = _run(cfg, sim_env(cfg, tmp_path))
    seeds = {i.repeat: i.seed for i in run.items}
    assert seeds == {1: 0, 2: 1, 3: 2}
    by_task: dict[str, set[str]] = {}
    for item in run.items:
        by_task.setdefault(item.task_id, set()).add(item.answer)
    assert any(len(answers) > 1 for answers in by_task.values())  # luck changes with the seed


# ---------------------------------------------------------------------------------- resume


def test_resume_after_a_kill_skips_finished_jobs_and_matches_an_unbroken_run(
    tmp_path: Path, dataset: Path
) -> None:
    # One job at a time, so a job's timings do not depend on which others were running with it.
    cfg = config(dataset, arms="solo-cheap,panel-cheap", concurrency=1)
    reference = _run(cfg, sim_env(cfg, tmp_path / "ref"), "ref")

    env = sim_env(cfg, tmp_path / "live")

    async def killed() -> None:
        seen = asyncio.Event()

        def on_item(item: BenchItem, progress: BenchProgress) -> None:
            if progress.done >= 5:
                seen.set()

        task = asyncio.ensure_future(run_bench(cfg, env=env, run_id="r1", on_item=on_item))
        await seen.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run_virtual(killed())
    store = BenchStore(tmp_path / "live")
    partial = store.get_run("r1")
    assert partial and partial.status == "interrupted"
    done_before = store.completed_keys("r1")
    assert 5 <= len(done_before) < reference.total_jobs

    fresh = sim_env(cfg, tmp_path / "live")  # a new process: nothing carried over in memory
    resumed = _run(cfg, fresh, "r1")
    calls_after = sum(len(s.calls) for s in fresh.sim_providers)
    assert resumed.status == "completed" and resumed.resumed_jobs == len(done_before)
    assert resumed.done_jobs == reference.total_jobs
    assert _stable(resumed) == _stable(reference)
    # Only the jobs that were not finished made any call.
    finished_calls = sum(i.metrics.calls for i in resumed.items if i.job_key in done_before)
    assert calls_after == sum(i.metrics.calls for i in resumed.items) - finished_calls


def test_resuming_a_complete_run_does_nothing(tmp_path: Path, dataset: Path) -> None:
    cfg = config(dataset, arms="solo-cheap")
    _run(cfg, sim_env(cfg, tmp_path))
    fresh = sim_env(cfg, tmp_path)
    again = _run(cfg, fresh)
    assert again.status == "completed" and again.resumed_jobs == again.total_jobs
    assert sum(len(s.calls) for s in fresh.sim_providers) == 0


def test_jobs_that_errored_are_tried_again_on_resume(
    tmp_path: Path, dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Flaky(PointsScorer):
        failures = 3

        async def score(self, task: Any, answer: Any, env: Any) -> Any:
            if Flaky.failures > 0:
                Flaky.failures -= 1
                raise RuntimeError("the scorer fell over")
            return await super().score(task, answer, env)

    monkeypatch.setitem(SCORERS, "code_review", Flaky())
    cfg = config(dataset, arms="solo-cheap", repeats=1, concurrency=1)
    env = sim_env(cfg, tmp_path)
    first = _run(cfg, env)
    assert first.failed_jobs == 3 and first.done_jobs == first.total_jobs - 3 == 1
    errored = [i for i in first.items if i.status == "error"]
    assert all("scoring failed" in (i.error or "") for i in errored)
    assert all(i.answer and i.score is None for i in errored)  # the answer is kept

    again = _run(cfg, sim_env(cfg, tmp_path))
    assert again.failed_jobs == 0 and again.done_jobs == again.total_jobs
    assert again.resumed_jobs == 1
    # The money of the failed attempts was spent, and is still in the run's total.
    assert again.spent_usd > sum(i.metrics.cost_usd for i in again.items)


def test_a_run_with_no_quorum_is_halted_and_finished(tmp_path: Path, dataset: Path) -> None:
    cfg = config(dataset, arms="panel-cheap", repeats=1, limit=1)
    env = sim_env(cfg, tmp_path)
    for sim in env.sim_providers:
        for model in sim.models.values():
            model.error_rate = 1.0
    run = _run(cfg, env)
    [item] = run.items
    assert item.status == "halted" and item.score is not None and item.score.quality == 0
    assert run.done_jobs == 1  # halted is a result (the arm failed the task), not an error


# ------------------------------------------------------------------------------------ caps


def test_the_run_cap_stops_cleanly_and_resume_with_more_money_finishes(
    tmp_path: Path, dataset: Path
) -> None:
    cfg = config(dataset, arms="solo-frontier,panel-cheap", repeats=2, concurrency=1)
    env = sim_env(cfg, tmp_path)
    full = _run(cfg, sim_env(cfg, tmp_path / "full"), "full")
    # Room for about three jobs: each is reserved at its worst case before it starts.
    tight = cfg.model_copy(update={"max_usd": 3.5 * worst_job_usd(cfg, env)})
    stopped = _run(tight, env, "r1")
    assert stopped.status == "stopped" and stopped.stop_reason and "max_usd" in stopped.stop_reason
    assert 0 < stopped.done_jobs < stopped.total_jobs
    assert stopped.spent_usd <= tight.max_usd + 1e-9  # one job at a time: no overshoot
    # Repeats come first, so what was done is whole repeats, not whole arms.
    assert {i.repeat for i in stopped.items} == {1}
    assert BenchStore(tmp_path).get_run("r1").status == "stopped"  # type: ignore[union-attr]

    finished = _run(cfg, sim_env(cfg, tmp_path), "r1")
    assert finished.status == "completed" and finished.done_jobs == finished.total_jobs
    assert finished.spent_usd == pytest.approx(full.spent_usd)


def test_concurrent_jobs_cannot_overshoot_the_cap_by_more_than_those_in_flight(
    tmp_path: Path, dataset: Path
) -> None:
    cfg = config(dataset, arms="panel-cheap", repeats=3, concurrency=4)
    full = _run(cfg, sim_env(cfg, tmp_path / "full"), "full")
    env = sim_env(cfg, tmp_path / "x")
    cap = full.spent_usd / 2
    assert cap > worst_job_usd(cfg, env)  # sanity: at least one job fits
    stopped = _run(cfg.model_copy(update={"max_usd": cap}), env, "x")
    biggest = max(i.metrics.cost_usd for i in full.items)
    assert stopped.status == "stopped"
    assert stopped.spent_usd <= cap + 4 * biggest


def test_the_live_spend_cap_stops_every_run_and_records_what_was_spent(tmp_path: Path) -> None:
    big = write_dataset(tmp_path / "big.jsonl", count=12)
    cfg = config(big, arms="panel-cheap", repeats=1, concurrency=1, mock=False)
    total = _run(cfg, sim_env(cfg, tmp_path / "ref"), "ref").spent_usd
    ledger = SpendLedger(tmp_path / "live" / "spend.json", cap_usd=total / 2)
    env = sim_env(cfg, tmp_path / "live", spend=ledger)
    stopped = _run(cfg, env, "live1")
    assert stopped.status == "stopped" and "live-spend cap" in (stopped.stop_reason or "")
    assert 0 < ledger.total() <= ledger.cap_usd + 1e-9
    assert ledger.total() == pytest.approx(stopped.spent_usd)
    assert {e["task"] for e in ledger.entries()} == {"bench"}
    assert all("live1" in e["purpose"] for e in ledger.entries())

    # A second run in the same session finds the cap already used up and refuses to start.
    second = _run(cfg, sim_env(cfg, tmp_path / "live", spend=ledger), "live2")
    assert second.done_jobs == 0 and second.status == "stopped"


def test_an_exhausted_spend_ledger_starts_no_job(tmp_path: Path, dataset: Path) -> None:
    cfg = config(dataset, arms="solo-cheap", mock=False)
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=1.0)
    ledger.append(1.0, task="x", purpose="earlier")
    env = sim_env(cfg, tmp_path, spend=ledger)
    run = _run(cfg, env)
    assert run.status == "stopped" and run.done_jobs == 0
    assert sum(len(s.calls) for s in env.sim_providers) == 0


def test_a_ledger_already_over_the_cap_refuses_to_start(tmp_path: Path, dataset: Path) -> None:
    cfg = config(dataset, arms="solo-cheap", mock=False)
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=1.0)
    ledger.append(1.5, task="x", purpose="earlier")
    env = sim_env(cfg, tmp_path, spend=ledger)
    with pytest.raises(ConfigError, match="live-spend cap"):
        _run(cfg, env)
    assert sum(len(s.calls) for s in env.sim_providers) == 0


def test_simulated_runs_never_touch_the_spend_ledger(tmp_path: Path, dataset: Path) -> None:
    cfg = config(dataset, arms="solo-cheap", mock=True)
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=0.0001)
    _run(cfg, sim_env(cfg, tmp_path, spend=ledger))
    assert not (tmp_path / "spend.json").exists()


# ------------------------------------------------------------------------------ concurrency


def test_no_more_jobs_run_at_once_than_the_concurrency_setting(
    tmp_path: Path, dataset: Path
) -> None:
    peaks = {}
    for width in (1, 3):
        cfg = config(dataset, arms="solo-cheap", repeats=3, concurrency=width)
        env = sim_env(cfg, tmp_path / str(width))
        _run(cfg, env)
        peaks[width] = max(s.max_in_flight for s in env.sim_providers)
    assert peaks == {1: 1, 3: 3}


def test_parallel_jobs_overlap_on_the_clock(tmp_path: Path, dataset: Path) -> None:
    cfg = config(dataset, arms="solo-cheap", repeats=1, concurrency=1)
    serial = _run(cfg, sim_env(cfg, tmp_path / "a"), "a")
    wide = cfg.model_copy(update={"concurrency": 4})
    parallel = _run(wide, sim_env(wide, tmp_path / "b"), "b")

    def results(run: BenchRun) -> dict[str, tuple[str, float, float | None]]:
        return {i.job_key: (i.answer, i.metrics.cost_usd, i.metrics.quality) for i in run.items}

    assert results(serial) == results(parallel)  # concurrency never changes an answer or a cost


# ------------------------------------------------------------------------------------ cache


def test_a_rerun_replays_from_the_cache_at_zero_cost_and_says_so(
    tmp_path: Path, dataset: Path
) -> None:
    cfg = config(dataset, arms="panel-cheap", repeats=2, mock=False)
    cache = ResponseDiskCache(tmp_path / "cache")

    def env_for(name: str) -> BenchEnv:
        env = sim_env(cfg, tmp_path / name, spend=SpendLedger(tmp_path / name / "spend.json"))
        wrapped = {n: CachingProvider(p, cache) for n, p in env.providers.items()}
        return BenchEnv(**{**env.__dict__, "providers": wrapped, "cache": cache})

    first = _run(cfg, env_for("one"), "one")
    assert first.cache_hits == 0 and first.spent_usd > 0
    assert all(i.metrics.latency_valid for i in first.items)  # repeats have their own seeds

    second = _run(cfg, env_for("two"), "two")
    assert second.spent_usd == 0
    assert second.cache_hits == sum(i.metrics.calls for i in second.items) > 0
    before = {i.job_key: i for i in first.items}
    for new in second.items:
        old = before[new.job_key]
        assert new.metrics.cost_usd == 0 and not new.metrics.latency_valid
        assert new.answer == old.answer and new.score == old.score
        assert all(c.cache_hit and c.cost_usd == 0 for c in new.calls)
    assert not (tmp_path / "two" / "spend.json").exists()  # a free rerun records no spend


# ------------------------------------------------------------------------- benchmark mode


class Spy(CachingProvider):
    """Wraps a provider and keeps every request it receives."""

    def __init__(self, inner: Any) -> None:
        super().__init__(inner, ResponseDiskCache(Path("/nonexistent")))
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await self.inner.safe_complete(request)


def _spied(env: BenchEnv) -> list[Spy]:
    spies = [Spy(p) for p in env.providers.values()]
    env.providers.update({s.name: s for s in spies})
    return spies


def test_benchmark_calls_stream_and_use_the_repeats_seed(tmp_path: Path, dataset: Path) -> None:
    cfg = config(dataset, arms="panel-cheap", repeats=2, limit=1)
    env = sim_env(cfg, tmp_path)
    spies = _spied(env)
    _run(cfg, env)
    requests = [r for s in spies for r in s.requests]
    assert requests and all(r.stream and r.temperature == 0.0 for r in requests)
    assert {r.seed for r in requests} == {0, 1}


def test_secrets_reach_the_models_untouched_unless_redaction_is_asked_for(tmp_path: Path) -> None:
    secret = "sk-ant-api03-" + "A" * 40
    rows = [
        {
            "id": "k",
            "category": "code_review",
            "prompt": f"Review this change that adds ANTHROPIC_API_KEY = {secret} to settings.py.",
            "context": "A settings module.",
            "truth": {"points": [point("a", "hard-coded")]},
        }
    ]
    path = tmp_path / "secret.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows))
    seen: dict[bool, bool] = {}
    for redact in (False, True):
        cfg = config(path, arms="solo-cheap", repeats=1, redact=redact)
        env = sim_env(cfg, tmp_path / str(redact))
        spies = _spied(env)
        _run(cfg, env)
        seen[redact] = any(secret in r.user_prompt for s in spies for r in s.requests)
    assert seen == {False: True, True: False}


def test_a_failed_run_becomes_an_error_item_that_still_counts_its_spend(
    tmp_path: Path, dataset: Path
) -> None:
    cfg = config(dataset, arms="panel-cheap", repeats=1, limit=1)
    env = sim_env(cfg, tmp_path)
    for sim in env.sim_providers:
        for model in sim.models.values():
            model.error_rate = 0.0
    # Make the synthesizer fail after the panel has answered and been paid for.
    original = {s: s._answer for s in env.sim_providers}
    for sim in env.sim_providers:

        def answer(
            request: ModelRequest, spec: Any, role: str, prompt: str, task: Any, *, _s: Any = sim
        ) -> str:
            if role == "synthesizer":
                raise RuntimeError("synthesizer exploded")
            return original[_s](request, spec, role, prompt, task)  # type: ignore[no-any-return]

        sim._answer = answer  # type: ignore[method-assign]
    run = _run(cfg, env)
    [item] = run.items
    assert item.status == "error" and "exploded" in (item.error or "")
    assert item.metrics.cost_usd > 0 and item.metrics.calls >= 3  # the panel's money is not lost
    assert run.failed_jobs == 1 and run.done_jobs == 0 and run.spent_usd == item.metrics.cost_usd


def test_a_halted_run_is_scored_and_marked_not_retried(tmp_path: Path) -> None:
    rows = [
        {
            "id": "h",
            "category": "code_review",
            "prompt": "Short.",  # too little context: the pipeline halts before any model call
            "truth": {"points": [point("a", "anything")]},
        }
    ]
    path = tmp_path / "h.jsonl"
    path.write_text(json.dumps(rows[0]) + "\n")
    cfg = config(path, arms="solo-cheap", repeats=1)
    run = _run(cfg, sim_env(cfg, tmp_path / "e"))
    [item] = run.items
    assert item.status == "halted" and item.metrics.calls == 0
    assert item.score is not None and item.score.quality == 0 and not item.metrics.solved
    assert run.done_jobs == 1 and run.failed_jobs == 0


def test_judge_models_must_be_in_the_catalog(tmp_path: Path, dataset: Path) -> None:
    cfg = config(dataset, judge_models=["no-such-model"])
    with pytest.raises(ConfigError, match="no-such-model"):
        _run(cfg, sim_env(cfg, tmp_path))


def test_unknown_arms_fail_before_anything_runs(tmp_path: Path, dataset: Path) -> None:
    cfg = config(dataset, arms="no-such-strategy")
    with pytest.raises(ConfigError, match="Unknown strategy"):
        _run(cfg, sim_env(cfg, tmp_path))
    assert not (tmp_path / "r1").exists()


async def test_providers_can_be_closed_after_a_run(tmp_path: Path, dataset: Path) -> None:
    cfg = config(dataset, arms="solo-cheap", repeats=1, limit=1)
    env = sim_env(cfg, tmp_path)
    await close_providers(env.providers)
