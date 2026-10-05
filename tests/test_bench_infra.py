"""The pieces a study stands on: the spend ledger, the response cache, the virtual clock, the
simulated provider, the scorer and the result store."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from _bench import point
from fusion.bench.cache import CachingProvider, ResponseDiskCache, request_digest
from fusion.bench.metrics import build_metrics
from fusion.bench.scoring import AnswerView, PointsScorer, ScoreEnv, is_solved
from fusion.bench.spec import BenchConfig, BenchTask, load_dataset
from fusion.bench.spend import LIVE_SPEND_CAP_USD, SpendCapError, SpendLedger
from fusion.bench.store import BenchItem, BenchStore
from fusion.bench.virtual import run_virtual
from fusion.config.catalog import load_catalog
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.providers.limits import ProviderLimiter
from fusion.providers.mock import MockProvider
from fusion.providers.simulated import (
    SimModel,
    SimulatedProvider,
    SimWorld,
    sim_models_from_catalog,
)
from fusion.storage.migrations import SCHEMA_VERSION
from fusion.telemetry.cost import PricingRegistry

# ------------------------------------------------------------------------------- spend ledger


def test_the_live_cap_is_twenty_dollars() -> None:
    assert LIVE_SPEND_CAP_USD == 20.0


def test_spend_is_appended_with_date_task_purpose_and_usd(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "bench-results" / "spend.json")
    assert ledger.total() == 0 and ledger.entries() == []
    ledger.append(1.25, task="5", purpose="catalog check")
    ledger.append(0.5, task="bench", purpose="run x")
    entries = ledger.entries()
    assert [e["usd"] for e in entries] == [1.25, 0.5]
    assert set(entries[0]) == {"date", "task", "purpose", "usd"}
    assert ledger.total() == pytest.approx(1.75)
    assert ledger.remaining() == pytest.approx(18.25)


def test_free_calls_are_not_recorded(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "spend.json")
    ledger.append(0.0, task="bench", purpose="cached")
    assert not (tmp_path / "spend.json").exists()


def test_the_cap_refuses_a_call_that_would_pass_it(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=2.0)
    ledger.append(1.5, task="bench", purpose="a")
    ledger.check(0.5)  # exactly reaching the cap is allowed
    with pytest.raises(SpendCapError, match=r"\$2\.00.*already spent"):
        ledger.check(0.6, "the next job")


def test_a_damaged_ledger_stops_live_calls_instead_of_resetting(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    path.write_text("{not json")
    with pytest.raises(SpendCapError, match="not valid JSON"):
        SpendLedger(path).check(0.0)
    path.write_text('{"usd": 1}')
    with pytest.raises(SpendCapError, match="array"):
        SpendLedger(path).total()


def test_appending_never_rewrites_earlier_entries(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "spend.json")
    ledger.append(1.0, task="a", purpose="first")
    first = ledger.entries()[0]
    ledger.append(2.0, task="b", purpose="second")
    assert ledger.entries()[0] == first


# ------------------------------------------------------------------------------ virtual clock


def test_a_virtual_hour_passes_instantly_and_in_order() -> None:
    async def scenario() -> tuple[float, list[str]]:
        loop = asyncio.get_running_loop()
        order: list[str] = []

        async def sleeper(name: str, seconds: float) -> None:
            await asyncio.sleep(seconds)
            order.append(name)

        await asyncio.gather(sleeper("slow", 3600), sleeper("fast", 10), sleeper("mid", 600))
        return loop.time(), order

    began = time.perf_counter()
    elapsed, order = run_virtual(scenario())
    assert elapsed == 3600  # the calls overlapped: one hour, not 4210 s
    assert order == ["fast", "mid", "slow"]
    assert time.perf_counter() - began < 1.0


def test_timeouts_and_cancelled_timers_work_on_the_virtual_clock() -> None:
    async def scenario() -> str:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.sleep(100), timeout=5)
        handle = asyncio.get_running_loop().call_later(50, lambda: None)
        handle.cancel()
        await asyncio.sleep(1)
        return "ok"

    assert run_virtual(scenario()) == "ok"


# ---------------------------------------------------------------------- simulated provider


def _world(tmp_path: Path | None = None, count: int = 40) -> tuple[SimWorld, list[BenchTask]]:
    tasks = [
        BenchTask.model_validate(
            {
                "id": f"s{n}",
                "category": "code_review",
                "prompt": f"Simulated review task number {n}.",
                "truth": {
                    "points": [point(f"p{n}", f"finding-{n}"), point(f"q{n}", f"other-{n}")],
                    "decoys": [point(f"d{n}", f"decoy-{n}")],
                },
            }
        )
        for n in range(count)
    ]
    return SimWorld(tasks), tasks


def _panel_request(task: BenchTask, model: str = "m", seed: int = 0) -> ModelRequest:
    return ModelRequest(
        model_id=model,
        user_prompt=f"## Task: code_review\n{task.prompt}\n",
        seed=seed,
        metadata={"role": "panel", "task_type": "code_review"},
        stream=True,
    )


async def _found(provider: SimulatedProvider, request: ModelRequest) -> set[str]:
    response = await provider.complete(request)
    claims = json.loads(response.text)["claims"]
    return {c["text"] for c in claims}


async def _rates(
    provider: SimulatedProvider, tasks: list[BenchTask], model: str
) -> dict[str, float]:
    hits = 0
    for task in tasks:
        text = " ".join(await _found(provider, _panel_request(task, model)))
        hits += f"finding-{task.id[1:]}" in text
    return {"recall": hits / len(tasks)}


def test_a_more_skilled_model_finds_more_points() -> None:
    world, tasks = _world(count=120)
    provider = SimulatedProvider(
        "sim", {"weak": SimModel(skill=0.2), "strong": SimModel(skill=0.9)}, world
    )
    weak = run_virtual(_rates(provider, tasks, "weak"))["recall"]
    strong = run_virtual(_rates(provider, tasks, "strong"))["recall"]
    assert strong > weak + 0.3
    assert 0.05 < weak < 0.45 and 0.7 < strong < 1.0


def test_answers_are_identical_for_the_same_seed_and_differ_for_another_repeat() -> None:
    world, tasks = _world(count=60)
    provider = SimulatedProvider("sim", {"m": SimModel(skill=0.5)}, world)

    async def answers(seed: int) -> list[set[str]]:
        return [await _found(provider, _panel_request(t, seed=seed)) for t in tasks]

    first, again, other = (run_virtual(answers(s)) for s in (0, 0, 1))
    assert first == again
    assert first != other  # luck changes with the repeat's seed


def test_models_of_one_family_make_correlated_mistakes() -> None:
    """If misses were independent, two 50%-skill models would both miss 25% of the time."""
    world, tasks = _world(count=400)
    spec = SimModel(skill=0.5, family="f")
    provider = SimulatedProvider("sim", {"a": spec, "b": spec}, world)

    async def both_missed() -> tuple[float, float]:
        miss_a = miss_b = both = 0
        for task in tasks:
            n = task.id[1:]
            a = f"finding-{n}" in " ".join(await _found(provider, _panel_request(task, "a")))
            b = f"finding-{n}" in " ".join(await _found(provider, _panel_request(task, "b")))
            miss_a += not a
            miss_b += not b
            both += (not a) and (not b)
        return miss_a * miss_b / len(tasks) ** 2, both / len(tasks)

    independent, observed = run_virtual(both_missed())
    assert observed > independent + 0.05


def test_cost_tokens_latency_and_speed_are_reported_like_a_real_provider() -> None:
    world, tasks = _world(count=1)
    spec = SimModel(
        skill=0.5,
        input_per_1m=2.0,
        output_per_1m=10.0,
        ttft_ms=1000,
        tokens_per_s=50,
        answer_tokens=500,
    )
    provider = SimulatedProvider("sim", {"m": spec}, world)

    async def call(stream: bool) -> ModelResponse:
        request = _panel_request(tasks[0]).model_copy(update={"stream": stream})
        return await provider.complete(request)

    streamed, plain = run_virtual(call(True)), run_virtual(call(False))
    assert streamed.input_tokens and streamed.output_tokens
    expected = (streamed.input_tokens * 2.0 + streamed.output_tokens * 10.0) / 1e6
    assert streamed.actual_cost_usd == pytest.approx(expected)
    assert streamed.latency_ms > 1000 * 0.9  # at least the time to first token
    assert streamed.ttft_ms and streamed.decode_tokens_per_s
    assert plain.ttft_ms is None and plain.decode_tokens_per_s is None
    assert plain.total_tokens_per_s


def test_a_simulated_model_can_fail() -> None:
    world, tasks = _world(count=1)
    provider = SimulatedProvider("sim", {"m": SimModel(error_rate=1.0)}, world)
    response = run_virtual(provider.safe_complete(_panel_request(tasks[0])))
    assert not response.ok and response.error_type == "Server"


def test_the_provider_limiter_caps_calls_in_flight_and_serialises_them() -> None:
    world, tasks = _world(count=1)

    async def scenario() -> tuple[int, float]:
        limiter = ProviderLimiter(max_concurrent=2, clock=asyncio.get_running_loop().time)
        provider = SimulatedProvider(
            "sim", {"m": SimModel(ttft_ms=10_000, tokens_per_s=1000)}, world, limiter=limiter
        )
        await asyncio.gather(
            *(provider.complete(_panel_request(tasks[0], seed=i)) for i in range(6))
        )
        return provider.max_in_flight, asyncio.get_running_loop().time()

    peak, elapsed = run_virtual(scenario())
    assert peak == 2
    assert 30 <= elapsed < 45  # three waves of about ten seconds


def test_simulated_catalog_models_carry_the_catalog_prices_and_tiers() -> None:
    catalog = load_catalog()
    models = sim_models_from_catalog(catalog, "anthropic")
    entry = catalog.models["claude-opus"]
    sim = models[entry.model_id]
    price = entry.price_at()
    assert price is not None
    assert (sim.input_per_1m, sim.output_per_1m) == (price.input_per_1m, price.output_per_1m)
    assert sim.skill > models[catalog.models["claude-haiku"].model_id].skill
    assert all(m.family == "anthropic" for m in models.values())


def test_roles_the_simulation_does_not_model_fall_back_to_the_mock() -> None:
    world, tasks = _world(count=1)
    provider = SimulatedProvider("sim", {}, world)
    request = ModelRequest(
        model_id="judge-x",
        user_prompt="Evaluate this answer",
        metadata={"role": "judge", "personality": "judge"},
    )
    response = run_virtual(provider.complete(request))
    assert "overall_score" in json.loads(response.text)


# ------------------------------------------------------------------------------- the cache


def _request(prompt: str = "hello", **kw: object) -> ModelRequest:
    return ModelRequest(model_id="m", user_prompt=prompt, **kw)  # type: ignore[arg-type]


def test_the_cache_key_covers_everything_that_decides_the_answer() -> None:
    base = request_digest("p", _request())
    assert base == request_digest("p", _request(timeout=5.0))  # patience does not matter
    assert base != request_digest("q", _request())
    for changed in (_request("bye"), _request(seed=1), _request(temperature=0.5)):
        assert base != request_digest("p", changed)


async def test_a_cached_answer_is_free_flagged_and_keeps_its_original_speed(tmp_path: Path) -> None:
    calls: list[ModelRequest] = []

    class Counting(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            calls.append(request)
            response = await super().complete(request)
            response.actual_cost_usd = 0.25
            response.latency_ms = 1234.0
            return response

    cache = ResponseDiskCache(tmp_path)
    provider = CachingProvider(Counting(latency_ms=0.0), cache)
    first = await provider.complete(_request("same"))
    second = await provider.complete(_request("same"))
    await provider.complete(_request("other"))
    assert len(calls) == 2 and (cache.hits, cache.misses) == (1, 2)
    assert not first.cache_hit and second.cache_hit
    assert second.text == first.text and second.latency_ms == 1234.0

    ledger = RunLedger()
    gateway = CallGateway(
        ledger=ledger, models={}, providers={"mock": provider}, pricing=PricingRegistry()
    )
    await gateway.call(stage="solo", alias="m", request=_request("same"), provider_name="mock")
    record = ledger.records[0]
    assert record.cache_hit and record.cost_usd == 0.0 and record.cost_known
    assert record.latency_ms == 1234.0
    metrics = build_metrics(ledger, wall_ms=5.0)
    assert metrics.cache_hits == 1 and not metrics.latency_valid


async def test_failures_are_never_cached(tmp_path: Path) -> None:
    class Failing(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            return ModelResponse(provider="mock", model="m", error="down", error_type="Server")

    cache = ResponseDiskCache(tmp_path)
    provider = CachingProvider(Failing(), cache)
    assert not (await provider.complete(_request())).ok
    assert not list(tmp_path.rglob("*.json"))


async def test_a_damaged_cache_file_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    cache = ResponseDiskCache(tmp_path)
    provider = CachingProvider(MockProvider(latency_ms=0.0), cache)
    await provider.complete(_request())
    for file in tmp_path.rglob("*.json"):
        file.write_text("{broken")
    again = await provider.complete(_request())
    assert again.ok and not again.cache_hit


# ----------------------------------------------------------------------------- the scorer


def _scored_task() -> BenchTask:
    return BenchTask.model_validate(
        {
            "id": "k",
            "category": "debugging",
            "prompt": "p",
            "truth": {
                "points": [
                    point("a", "deadlock"),
                    {**point("b", "lock order"), "weight": 3.0},
                ],
                "decoys": [point("d", "cosmic ray")],
            },
        }
    )


async def _score(answer: str) -> float:
    gateway = CallGateway(ledger=RunLedger(), models={}, providers={}, pricing=PricingRegistry())
    result = await PointsScorer().score(_scored_task(), AnswerView(answer), ScoreEnv(gateway))
    return result.quality


async def test_the_points_scorer_weighs_recall_and_penalises_decoys() -> None:
    assert await _score("A DEADLOCK between workers; fix the lock order") == 1.0
    assert await _score("a deadlock") == pytest.approx(0.25)  # weight 1 of 4
    assert await _score("lock order") == pytest.approx(0.75)
    assert await _score("nothing useful") == 0.0
    assert await _score("deadlock and lock order, caused by a cosmic ray") == pytest.approx(0.5)
    assert await _score("cosmic ray") == 0.0  # the penalty never goes below zero


async def test_the_scorer_ignores_claims_the_aggregator_dropped() -> None:
    gateway = CallGateway(ledger=RunLedger(), models={}, providers={}, pricing=PricingRegistry())
    view = AnswerView("nothing", claims=[{"text": "deadlock and lock order"}])
    result = await PointsScorer().score(_scored_task(), view, ScoreEnv(gateway))
    assert result.quality == 0.0


def test_solved_means_quality_reaches_the_category_threshold() -> None:
    assert is_solved("debugging", 0.6) and not is_solved("debugging", 0.59)
    assert not is_solved("debugging", None)


# ---------------------------------------------------------------------------------- the store


def _item(run_id: str, key: str, status: str = "completed", cost: float = 0.1) -> BenchItem:
    ledger = RunLedger()
    metrics = build_metrics(ledger, wall_ms=1000.0, quality=0.5, solved=False)
    return BenchItem(
        run_id=run_id,
        job_key=key,
        task_id="t",
        category="debugging",
        arm="a",
        repeat=1,
        seed=0,
        status=status,  # type: ignore[arg-type]
        metrics=metrics.model_copy(update={"cost_usd": cost}),
    )


def _config(tmp_path: Path) -> BenchConfig:
    from _bench import config, write_dataset

    return config(write_dataset(tmp_path / "d.jsonl"))


def test_items_survive_a_crash_and_a_torn_last_line(tmp_path: Path) -> None:
    store = BenchStore(tmp_path)
    store.create_run("r", _config(tmp_path), total_jobs=3)
    store.add_item(_item("r", "k1"))
    store.add_item(_item("r", "k2", status="error"))
    results = tmp_path / "r" / "results.jsonl"
    with results.open("a") as handle:
        handle.write('{"run_id": "r", "job_key": "k3", "tr')  # killed mid-write
    reopened = BenchStore(tmp_path)
    assert [i.job_key for i in reopened.items("r")] == ["k1", "k2"]
    assert reopened.completed_keys("r") == {"k1"}  # an error is not finished: resume retries it


def test_the_latest_attempt_wins_but_all_spending_is_counted(tmp_path: Path) -> None:
    store = BenchStore(tmp_path)
    store.create_run("r", _config(tmp_path), total_jobs=1)
    store.add_item(_item("r", "k", status="error", cost=0.3))
    store.add_item(_item("r", "k", status="completed", cost=0.0))
    assert [i.status for i in store.items("r")] == ["completed"]
    assert store.spent("r") == (pytest.approx(0.3), 0.0)


def test_runs_are_listed_newest_first_and_indexed_in_sqlite(tmp_path: Path) -> None:
    store = BenchStore(tmp_path)
    store.create_run("a", _config(tmp_path), total_jobs=2)
    store.create_run("b", _config(tmp_path), total_jobs=2)
    store.add_item(_item("a", "k1"))
    store.update_run("a", status="stopped", stop_reason="cap", done_jobs=1, spent_usd=0.1)
    assert [r.run_id for r in store.list_runs()] == ["b", "a"]
    record = store.get_run("a")
    assert record and (record.status, record.stop_reason, record.done_jobs) == ("stopped", "cap", 1)
    rows = store._db().execute("SELECT job_key, arm, solved FROM bench_items").fetchall()
    assert [tuple(r) for r in rows] == [("k1", "a", 0)]
    assert (
        store._db().execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        == SCHEMA_VERSION
    )


def test_sync_index_rebuilds_rows_lost_between_a_write_and_its_indexing(tmp_path: Path) -> None:
    store = BenchStore(tmp_path)
    store.create_run("r", _config(tmp_path), total_jobs=1)
    item = _item("r", "k")
    with (tmp_path / "r" / "results.jsonl").open("a") as handle:  # written, never indexed
        handle.write(item.model_dump_json() + "\n")
    assert store._db().execute("SELECT COUNT(*) FROM bench_items").fetchone()[0] == 0
    store.sync_index("r")
    assert store._db().execute("SELECT COUNT(*) FROM bench_items").fetchone()[0] == 1


def test_an_unknown_run_says_which_exist(tmp_path: Path) -> None:
    store = BenchStore(tmp_path)
    store.create_run("known", _config(tmp_path), total_jobs=1)
    with pytest.raises(FileNotFoundError, match="known"):
        store.load_config("missing")


def test_the_toy_dataset_has_ground_truth_the_simulation_can_use() -> None:
    tasks = load_dataset("toy")
    world = SimWorld(tasks)
    for task in tasks:
        found = world.task_for(f"## Task: x\n{task.prompt}\n## Additional Context\n{task.context}")
        assert found is not None and found.id == task.id
