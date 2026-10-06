"""A replayed call is free to the run and not free to its strategy; `resume --retry-halted`; the
default strategy; the stand-alone chart."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from _bench import config, sim_env, write_dataset
from _stats import item
from fusion.bench.cache import CachingProvider, ResponseDiskCache
from fusion.bench.costing import full_cost_usd
from fusion.bench.metrics import build_metrics
from fusion.bench.report import build_report
from fusion.bench.report.html import cost_quality_svg
from fusion.bench.runner import run_bench
from fusion.bench.stats import Rules, study_stats
from fusion.bench.store import BenchStore
from fusion.bench.virtual import run_virtual
from fusion.cli.app import app
from fusion.orchestration.ledger import CallGateway, CallRecord, RunLedger
from fusion.orchestration.strategy import load_strategy_book
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.providers.mock import MockProvider
from fusion.telemetry.cost import PricingRegistry

runner = CliRunner()
FAST = Rules(n_boot=200)


def _request(text: str = "hi") -> ModelRequest:
    return ModelRequest(model_id="m", system_prompt="s", user_prompt=text, max_tokens=10)


# ----------------------------------------------------------------------------- the ledger


async def test_a_replayed_call_is_billed_at_zero_but_keeps_its_list_price(tmp_path: Path) -> None:
    class Priced(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            response = await super().complete(request)
            response.actual_cost_usd = 0.25
            return response

    provider = CachingProvider(Priced(latency_ms=0.0), ResponseDiskCache(tmp_path))
    ledger = RunLedger()
    gateway = CallGateway(
        ledger=ledger, models={}, providers={"mock": provider}, pricing=PricingRegistry()
    )
    for _ in range(2):
        await gateway.call(stage="solo", alias="m", request=_request(), provider_name="mock")
    paid, replayed = ledger.records
    assert (paid.cost_usd, paid.list_cost_usd, paid.cache_hit) == (0.25, 0.25, False)
    assert (replayed.cost_usd, replayed.list_cost_usd, replayed.cache_hit) == (0.0, 0.25, True)
    metrics = build_metrics(ledger, wall_ms=1000.0)
    assert metrics.cost_usd == 0.25  # what was billed: the spend ledger counts this
    assert metrics.replayed_cost_usd == 0.25 and metrics.cache_hits == 1


def test_a_missing_provider_is_free_and_has_no_list_price() -> None:
    record = CallRecord(stage="solo", model_alias="m", provider="x", cost_usd=0.0)
    assert record.list_cost_usd is None


# ------------------------------------------------------------------------------ full cost


def test_full_cost_is_the_bill_plus_what_the_replays_would_have_cost() -> None:
    fresh = item("a", "t1", 0.9, cost=0.10)
    assert full_cost_usd(fresh) == pytest.approx(0.10)
    replayed = item("a", "t2", 0.9, cost=0.04, cache_hits=2, replayed_cost_usd=0.06)
    assert full_cost_usd(replayed) == pytest.approx(0.10)


def test_items_stored_before_list_prices_existed_are_priced_from_their_tokens() -> None:
    old = item("a", "t1", 0.9, cost=0.0, cache_hits=1)
    old.calls = [
        CallRecord(
            stage="solo",
            model_alias="claude-haiku",
            provider="anthropic",
            model_id="claude-haiku-4-5-20251001",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cost_usd=0.0,
            cache_hit=True,
        )
    ]
    assert full_cost_usd(old) == pytest.approx(6.0)  # $1 in + $5 out per million tokens
    unknown = old.model_copy(deep=True)
    unknown.calls[0].input_tokens = None
    assert full_cost_usd(unknown) == 0.0


def test_stats_charge_an_arm_for_calls_another_arm_paid_for() -> None:
    items = [item("paid", f"t{n}", 0.8, cost=0.10) for n in range(12)]
    items += [
        item("replaying", f"t{n}", 0.8, cost=0.02, cache_hits=3, replayed_cost_usd=0.08)
        for n in range(12)
    ]
    stats = study_stats(items, baseline="paid", rules=FAST)
    by_arm = {a.arm: a for a in stats.arms}
    assert by_arm["replaying"].cost_per_task is not None
    assert by_arm["replaying"].cost_per_task.estimate == pytest.approx(0.10)  # not 0.02
    assert by_arm["replaying"].cost_total_usd == pytest.approx(1.20)
    cheaper = next(v for v in stats.comparisons[0].verdicts if v.claim == "cheaper")
    assert cheaper.outcome != "yes"  # equal cost is not "cheaper"


# --------------------------------------------------------------------- retry halted jobs


def _halt_everything(env: Any) -> None:
    for sim in env.sim_providers:
        for model in sim.models.values():
            model.error_rate = 1.0


def test_resume_can_run_halted_jobs_again_and_the_report_says_so(tmp_path: Path) -> None:
    data = write_dataset(tmp_path / "data.jsonl", count=4)
    cfg = config(data, arms="panel-cheap", repeats=1)
    env = sim_env(cfg, tmp_path / "r")
    _halt_everything(env)
    first = run_virtual(run_bench(cfg, env=env, run_id="r1"))
    assert first.done_jobs == 4 and {i.status for i in first.items} == {"halted"}

    plain = run_virtual(run_bench(cfg, env=sim_env(cfg, tmp_path / "r"), run_id="r1"))
    assert plain.resumed_jobs == 4 and {i.status for i in plain.items} == {"halted"}

    retried = run_virtual(
        run_bench(cfg, env=sim_env(cfg, tmp_path / "r"), run_id="r1", retry_halted=True)
    )
    assert retried.resumed_jobs == 0 and {i.status for i in retried.items} == {"completed"}
    store = BenchStore(tmp_path / "r")
    assert store.retried_after_halt("r1") == {"panel-cheap": 4}
    record = store.get_run("r1")
    assert record is not None
    report = build_report(
        record,
        store.items("r1"),
        retried_after_halt=store.retried_after_halt("r1"),
        rules=FAST,
    )
    assert any("4 job(s) halted on their first attempt" in n for n in report.notes)
    assert any("panel-cheap (4)" in n for n in report.notes)


def test_a_run_nobody_retried_has_no_retry_note(tmp_path: Path) -> None:
    data = write_dataset(tmp_path / "data.jsonl", count=3)
    cfg = config(data, arms="solo-cheap", repeats=1)
    run_virtual(run_bench(cfg, env=sim_env(cfg, tmp_path / "r"), run_id="r1"))
    assert BenchStore(tmp_path / "r").retried_after_halt("r1") == {}


def test_the_resume_command_has_the_option(fusion_home: Path) -> None:
    runner.invoke(
        app,
        [
            "bench",
            "run",
            "-d",
            "toy",
            "--arms",
            "solo-cheap",
            "--mock",
            "--limit",
            "2",
            "--repeats",
            "1",
        ],
    )
    run_id = next(p.name for p in (fusion_home / "project" / "bench-results").glob("bench-*"))
    result = runner.invoke(app, ["bench", "resume", run_id, "--retry-halted"])
    assert result.exit_code == 0, result.output
    assert "completed" in result.output


# ---------------------------------------------------------------------- the default strategy


def test_the_default_strategy_is_the_two_model_panel_the_study_chose() -> None:
    book = load_strategy_book()
    assert book.for_budget("medium").name == "panel-duo"
    duo = book.get("panel-duo")
    assert [m.model for m in duo.members] == ["claude-haiku", "gpt-luna"]
    assert duo.aggregator == "llm" and duo.aggregator_model == "claude-haiku"
    assert [m.model for m in book.get("panel-cheap").members] == [
        "claude-haiku",
        "gpt-luna",
        "gemini-flash",
    ]  # the three-model panel is still there, by name


def test_the_headline_suite_arm_fusion_best_is_that_same_panel() -> None:
    from fusion.bench.arms import resolve_arm
    from fusion.bench.suite import load_suite

    cfg = load_suite("headline").config
    arm = next(a for a in cfg["arms"] if a["name"] == "fusion-best")
    from fusion.bench.spec import Arm

    resolved = resolve_arm(Arm.model_validate(arm), load_strategy_book())
    assert [m.model for m in resolved.members] == [
        m.model for m in load_strategy_book().get("panel-duo").members
    ]


# -------------------------------------------------------------------------- the SVG chart


def test_the_stand_alone_chart_is_valid_svg_with_its_own_styles(tmp_path: Path) -> None:
    from _stats import two_arms
    from fusion.bench.store import BenchRunRecord

    cfg = config(Path("x"), arms="solo-cheap")
    record = BenchRunRecord(
        run_id="r", status="completed", dataset="x", mock=True, config=cfg, total_jobs=1,
        done_jobs=1, spent_usd=0.0, eval_spent_usd=0.0, created_at="", updated_at="",
    )  # fmt: skip
    report = build_report(record, two_arms(12), baseline="solo", rules=FAST)
    svg = cost_quality_svg(report)
    root = ET.fromstring(svg)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert "<style>" in svg and "prefers-color-scheme:dark" in svg and "--c1:" in svg
    assert "solo" in svg and "fusion" in svg and 'class="frontier"' in svg
    assert "<script" not in svg and "href=" not in svg


def test_results_can_write_the_chart_beside_the_document(fusion_home: Path) -> None:
    runner.invoke(app, ["bench", "run", "--suite", "latency", "--mock", "--limit", "12", "--yes"])
    run_id = next(p.name for p in (fusion_home / "project" / "bench-results").glob("bench-*"))
    chart = fusion_home / "assets" / "chart.svg"
    result = runner.invoke(
        app,
        ["bench", "results", run_id, "-o", str(fusion_home / "r.md"), "--svg", str(chart)],
    )
    assert result.exit_code == 0, result.output
    assert ET.fromstring(chart.read_text()).tag.endswith("svg")
    assert f"--svg {chart}" in (fusion_home / "r.md").read_text()
