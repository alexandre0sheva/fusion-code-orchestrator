"""`fusion bench plan` and the `fusion bench` commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from _bench import config, sim_env, write_dataset
from fusion.bench.arms import arm_book
from fusion.bench.plan import estimate_job, make_plan
from fusion.bench.runner import BenchEnv, run_bench
from fusion.bench.scoring import get_scorer
from fusion.bench.spec import load_dataset
from fusion.bench.virtual import run_virtual
from fusion.cli.app import app
from fusion.routing.policy import RoutingPolicy
from fusion.telemetry.cost import PricingRegistry

runner = CliRunner()


def _plan(cfg: Any, env: BenchEnv, *, spend_left: float | None = None, pricing: Any = None) -> Any:
    book = arm_book(env.book, cfg.arms)
    routing = RoutingPolicy(env.routing_config, registry=env.registry, strategies=book)
    return (
        make_plan(
            cfg,
            load_dataset(cfg.dataset),
            {a.name: book.get(a.name) for a in cfg.arms},
            routing=routing,
            registry=env.registry,
            pricing=pricing or env.pricing,
            spend_left_usd=spend_left,
        ),
        book,
        routing,
    )


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    return write_dataset(tmp_path / "data.jsonl", count=6)


# ------------------------------------------------------------------------------------ plan


def test_the_plan_prices_every_arm_with_a_range_around_the_expectation(
    tmp_path: Path, dataset: Path
) -> None:
    cfg = config(dataset, arms="default", repeats=3, mock=False, max_usd=1000)
    plan, *_ = _plan(cfg, sim_env(cfg, tmp_path))
    assert plan.jobs == 6 * 6 * 3 and plan.tasks == 6 and plan.repeats == 3
    assert plan.low_usd < plan.expected_usd < plan.high_usd
    assert plan.expected_usd == pytest.approx(sum(a.expected_usd for a in plan.per_arm))
    by_arm = {a.arm: a.expected_usd for a in plan.per_arm}
    assert by_arm["solo-cheap"] < by_arm["panel-cheap"] < by_arm["panel-refine"]
    assert by_arm["solo-frontier"] > by_arm["solo-cheap"]
    assert plan.wall_seconds > 0 and plan.fits and plan.suggestion is None


def test_the_estimate_is_in_the_right_range_of_what_a_simulated_run_spends(
    tmp_path: Path, dataset: Path
) -> None:
    cfg = config(dataset, arms="solo-cheap,panel-cheap,panel-refine", repeats=2)
    env = sim_env(cfg, tmp_path)
    plan, *_ = _plan(cfg, env)
    actual = run_virtual(run_bench(cfg, env=env, run_id="r")).spent_usd
    assert plan.low_usd <= actual <= plan.high_usd


def test_a_cascade_is_priced_between_its_first_wave_and_its_full_panel(
    tmp_path: Path, dataset: Path
) -> None:
    cfg = config(dataset, arms="panel-cascade,panel-cheap")
    env = sim_env(cfg, tmp_path)
    _, book, routing = _plan(cfg, env)
    task = load_dataset(dataset)[0]
    est = estimate_job(
        task,
        book.get("panel-cascade"),
        routing=routing,
        registry=env.registry,
        pricing=env.pricing,
        scorer=get_scorer(task.category),
    )
    assert est.low_usd < est.expected_usd < est.worst_usd <= est.high_usd


def test_a_plan_over_budget_suggests_fewer_repeats_first(tmp_path: Path, dataset: Path) -> None:
    cfg = config(dataset, arms="default", repeats=3, mock=False, max_usd=1000)
    env = sim_env(cfg, tmp_path)
    full, *_ = _plan(cfg, env)
    budget = full.expected_usd * 0.45  # two repeats cost 2/3, one repeat costs 1/3
    tight = cfg.model_copy(update={"max_usd": budget})
    plan, *_ = _plan(tight, env)
    assert not plan.fits and plan.suggestion is not None
    assert (plan.suggestion.repeats, plan.suggestion.limit) == (1, None)
    assert plan.suggestion.arms == plan.arms and plan.suggestion.expected_usd <= budget


def test_then_fewer_tasks_then_fewer_arms(tmp_path: Path) -> None:
    big = write_dataset(tmp_path / "big.jsonl", count=40)
    cfg = config(big, arms="default", repeats=3, mock=False, max_usd=1000)
    env = sim_env(cfg, tmp_path)
    full, *_ = _plan(cfg, env)
    one_repeat = full.expected_usd / 3

    fewer_tasks, *_ = _plan(cfg.model_copy(update={"max_usd": one_repeat * 0.5}), env)
    s = fewer_tasks.suggestion
    assert s is not None and s.repeats == 1 and s.arms == fewer_tasks.arms
    assert s.limit is not None and 10 <= s.limit < 40

    fewer_arms, *_ = _plan(cfg.model_copy(update={"max_usd": one_repeat * 0.05}), env)
    s = fewer_arms.suggestion
    assert s is not None and s.repeats == 1 and len(s.arms) < 6
    assert "solo-frontier" not in s.arms  # the dearest arm goes first
    assert s.expected_usd <= one_repeat * 0.05

    nothing, *_ = _plan(cfg.model_copy(update={"max_usd": 0.00001}), env)
    assert nothing.suggestion is None and not nothing.fits


def test_the_budget_is_the_smaller_of_max_usd_and_what_the_spend_cap_has_left(
    tmp_path: Path, dataset: Path
) -> None:
    cfg = config(dataset, arms="solo-cheap", mock=False, max_usd=100)
    env = sim_env(cfg, tmp_path)
    plan, *_ = _plan(cfg, env, spend_left=0.001)
    assert plan.budget_usd == 0.001 and not plan.fits


def test_a_model_without_a_price_fails_a_live_plan_but_not_a_simulated_one(
    tmp_path: Path, dataset: Path
) -> None:
    catalog = PricingRegistry().catalog
    models = dict(catalog.models)
    models["claude-haiku"] = models["claude-haiku"].model_copy(update={"prices": []})
    unpriced = PricingRegistry(catalog.model_copy(update={"models": models}))
    live = config(dataset, arms="solo-cheap", mock=False)
    plan, *_ = _plan(live, sim_env(live, tmp_path), pricing=unpriced)
    assert plan.unpriced == ["claude-haiku"] and not plan.fits
    mock = config(dataset, arms="solo-cheap", mock=True)
    simulated, *_ = _plan(mock, sim_env(mock, tmp_path), pricing=unpriced)
    assert simulated.fits


# ------------------------------------------------------------------------------------- cli


def _invoke(*args: str) -> Any:
    return runner.invoke(app, ["bench", *args])


def test_bench_run_mock_works_end_to_end_on_the_toy_dataset(fusion_home: Path) -> None:
    result = _invoke("run", "--dataset", "toy", "--arms", "default", "--mock", "--repeats", "1")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "The plan fits" in out and "simulated" in out
    assert "completed" in out and "48/48" in out and "solo-frontier" in out
    runs = list((fusion_home / "project" / "bench-results").glob("bench-*"))
    assert len(runs) == 1 and (runs[0] / "results.jsonl").exists()
    assert not (fusion_home / "project" / "bench-results" / "spend.json").exists()


def test_list_show_and_resume_work_on_a_finished_run(fusion_home: Path) -> None:
    assert _invoke("list").output.strip().startswith("No benchmark runs yet")
    _invoke("run", "-d", "toy", "--arms", "solo-cheap,panel-cheap", "--mock", "--repeats", "1")
    listed = _invoke("list")
    assert "mock" in listed.output and "16/16" in listed.output and "completed" in listed.output
    run_id = next(p.name for p in (fusion_home / "project" / "bench-results").glob("bench-*"))

    shown = _invoke("show", run_id)
    assert shown.exit_code == 0 and "panel-cheap" in shown.output
    payload = json.loads(_invoke("show", run_id, "--json").output)
    assert payload["run"]["status"] == "completed" and len(payload["arms"]) == 2
    assert {a["arm"] for a in payload["arms"]} == {"solo-cheap", "panel-cheap"}

    resumed = _invoke("resume", run_id)
    assert resumed.exit_code == 0 and "16/16" in resumed.output


def test_show_and_resume_name_the_runs_that_exist_when_the_id_is_wrong(fusion_home: Path) -> None:
    _invoke("run", "-d", "toy", "--arms", "solo-cheap", "--mock", "--repeats", "1", "--limit", "2")
    for command in ("show", "resume"):
        result = _invoke(command, "no-such-run")
        assert result.exit_code == 1 and "no-such-run" in result.output
        assert "bench-" in result.output  # the recent runs are listed


def test_plan_says_whether_a_live_study_fits_without_needing_api_keys(fusion_home: Path) -> None:
    result = _invoke("plan", "-d", "toy", "--arms", "default", "--max-usd", "50")
    assert result.exit_code == 0 and "The plan fits" in result.output
    tight = _invoke("plan", "-d", "toy", "--arms", "default", "--max-usd", "0.05")
    assert tight.exit_code == 2 and "does not fit" in tight.output
    assert "fusion bench run" in tight.output and "--repeats 1" in tight.output


def test_a_live_run_that_does_not_pass_the_plan_will_not_start_without_yes(
    fusion_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fusion.bench import cli
    from fusion.bench import runner as bench_runner

    seen: list[str] = []

    def providers_without_keys(**_: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(bench_runner, "build_provider_registry", providers_without_keys)

    def fake_execute(cfg: Any, env: Any, run_id: Any) -> Any:
        seen.append("executed")
        raise SystemExit(0)

    monkeypatch.setattr(cli, "_execute", fake_execute)
    refused = _invoke("run", "-d", "toy", "--arms", "default", "--max-usd", "0.05")
    assert refused.exit_code == 2 and "Not starting" in refused.output and not seen
    forced = _invoke("run", "-d", "toy", "--arms", "default", "--max-usd", "0.05", "--yes")
    assert seen == ["executed"] and forced.exit_code == 0


def test_a_live_run_needs_an_explicit_spending_limit_and_api_keys(fusion_home: Path) -> None:
    missing = _invoke("run", "-d", "toy", "--arms", "solo-cheap")
    assert missing.exit_code == 1 and "--max-usd is required" in missing.output
    no_keys = _invoke("run", "-d", "toy", "--arms", "solo-cheap", "--max-usd", "1")
    assert no_keys.exit_code == 1 and "No cloud providers configured" in no_keys.output


def test_config_file_supplies_defaults_that_options_override(
    fusion_home: Path, tmp_path: Path
) -> None:
    spec = tmp_path / "study.yaml"
    spec.write_text(
        "dataset: toy\n"
        "mock: true\n"
        "repeats: 1\n"
        "arms:\n"
        "  - {name: solo-cheap, strategy: solo-cheap}\n"
        "  - {name: refine-3, strategy: panel-refine, overrides: {rounds: 3}}\n"
    )
    result = _invoke("run", "--config", str(spec), "--limit", "2")
    assert result.exit_code == 0, result.output
    assert "refine-3" in result.output and "4/4" in result.output  # 2 tasks x 2 arms x 1 repeat


def test_missing_arguments_are_explained(fusion_home: Path) -> None:
    result = _invoke("run", "--mock")
    assert result.exit_code == 1 and "--dataset and --arms" in result.output


def test_spend_command_reports_the_ledger(fusion_home: Path) -> None:
    from fusion.bench.spend import default_ledger

    default_ledger().append(1.5, task="5", purpose="check")
    out = _invoke("spend").output
    assert "$1.5000 of $20.00" in out and "1 entries" in out
