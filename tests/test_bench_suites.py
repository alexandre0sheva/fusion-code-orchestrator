"""Suites, quotas, the spend stop line, per-strategy fan-out controls and the sample aliases.

Everything is offline: suites are read, priced by the planner and dry-run on simulated models.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from _bench import config, sim_env, write_dataset
from fusion.bench.arms import arm_book, resolve_arm
from fusion.bench.judges import arm_aliases
from fusion.bench.runner import build_env, run_bench
from fusion.bench.spec import BenchConfig, BenchTask, load_dataset, select_tasks
from fusion.bench.spend import SpendLedger
from fusion.bench.suite import SUITES_DIR, list_suites, load_suite
from fusion.bench.virtual import run_virtual
from fusion.cli.app import app
from fusion.config.catalog import load_catalog
from fusion.config.layers import ConfigError
from fusion.config.loader import EarlyReturn, FanoutConfig
from fusion.orchestration.stages import PanelStage
from fusion.orchestration.strategy import FanoutOverride, load_strategy_book
from fusion.routing.policy import RoutingPolicy

runner = CliRunner()
SUITES = (
    "ablation",
    "headline",
    "latency",
    "ablation-v3",
    "headline-v3",
    "latency-v3",
    "route-v3",
)
V3 = ("ablation-v3", "headline-v3", "latency-v3")  # on dataset v2, for roadmap Task 16


def _bench_config(name: str) -> BenchConfig:
    data = dict(load_suite(name).config)
    data["dataset"] = f"evals/datasets/{data['dataset']}"
    return BenchConfig.model_validate(data)


# ------------------------------------------------------------------------------- the files


def test_the_packaged_suites_are_the_ones_the_roadmap_names() -> None:
    assert {s.name for s in list_suites()} == set(SUITES)
    assert {p.stem for p in SUITES_DIR.glob("*.yaml")} == set(SUITES)
    for suite in list_suites():
        assert suite.description and suite.stage and suite.arms


def test_ablation_expands_to_the_arms_the_roadmap_lists() -> None:
    arms = load_suite("ablation").arms
    assert arms[0] == "size-3"  # the default panel comes first: it is the report's baseline
    assert {"size-1", "size-2", "size-3", "size-5"} <= set(arms)  # panel size 1 / 2 / 3 / 5
    assert {"refine-on", "synth-strong"} <= set(arms)  # refinement, cheap vs strong synthesizer
    assert {"cascade-0.5", "cascade-0.7"} <= set(arms)  # the threshold sweep
    # Lenses and union (Task 7): on/off, union with and without the check call, and the
    # self-mixtures, whose diversity now comes from lenses (Claude takes no sampling parameters).
    assert {"lenses-off", "union", "union-check", "self-luna", "self-haiku"} <= set(arms)
    assert {"auto-route", "auto-route-frontier"} <= set(arms)  # difficulty routing (Task 9)
    assert len(arms) == len(set(arms))


def test_headline_has_the_six_arms_and_is_a_study_on_the_held_out_split() -> None:
    suite = load_suite("headline")
    assert suite.arms == [
        "solo-opus",
        "solo-sol",
        "solo-haiku",
        "solo-luna",
        "fusion-default",
        "fusion-best",
    ]
    assert suite.config["split"] == "test" and suite.config["repeats"] >= 2


def test_the_tuning_suites_never_touch_the_test_split() -> None:
    assert load_suite("ablation").config["split"] == "dev"
    assert load_suite("latency").config["split"] == "dev"


def test_the_latency_suite_measures_time_so_the_cache_is_off() -> None:
    cfg = _bench_config("latency")
    assert cfg.cache is False
    book = arm_book(load_strategy_book(), cfg.arms)
    assert book.get("solo-opus").kind == "solo"  # the median the default must match
    default = book.get("default")
    assert default.fanout is None and default.skip_aggregator is not None  # as shipped
    assert book.get("panel-duo").members == default.members  # the panel default, as shipped
    assert book.get("auto-route").kind == "auto"  # strategy auto, the default since Task 9
    off = book.get("levers-off")
    assert off.skip_aggregator is None
    assert off.fanout == FanoutOverride(early_return="off", hedge_after_ms="off")
    base = FanoutConfig(early_return=EarlyReturn(), hedge_after_ms=9000)
    stripped = off.fanout.applied_to(base)
    assert stripped.early_return is None and stripped.hedge_after_ms is None
    luna = next(m for m in book.get("luna-none").members if m.model == "gpt-luna")
    assert luna.reasoning_effort == "none"


@pytest.mark.parametrize("name", SUITES)
def test_every_arm_resolves_to_a_valid_strategy_over_catalog_models(name: str) -> None:
    cfg = _bench_config(name)
    book = load_strategy_book()
    catalog = load_catalog()
    for arm in cfg.arms:
        strategy = resolve_arm(arm, book)
        for alias in strategy.models:
            assert alias in catalog.models and catalog.models[alias].enabled, (arm.name, alias)
            assert catalog.models[alias].price_at() is not None, alias


@pytest.mark.parametrize("name", SUITES)
def test_a_suite_only_asks_for_tasks_that_exist_and_stays_in_its_split(name: str) -> None:
    cfg = _bench_config(name)
    tasks = load_dataset(cfg.dataset, cfg.split)
    available = Counter(t.category for t in tasks)
    for category, wanted in cfg.quota.items():
        assert available[category] >= wanted, f"{name}: only {available[category]} {category}"
    chosen = select_tasks(tasks, cfg.limit, cfg.seed, cfg.quota)
    assert len(chosen) == sum(cfg.quota.values())
    assert {t.split for t in chosen} <= {cfg.split, None}
    text_only = name.startswith(("latency", "route"))  # wall time and route calibration: text
    assert {"coding", "frontend", "performance"} <= set(cfg.quota) or text_only


@pytest.mark.parametrize(
    ("tune", "final"), [("ablation", "headline"), ("ablation-v3", "headline-v3")]
)
def test_the_dev_and_test_subsets_do_not_share_a_task(tune: str, final: str) -> None:
    tuning, held_out = _bench_config(tune), _bench_config(final)
    dev = select_tasks(load_dataset(tuning.dataset, "dev"), None, tuning.seed, tuning.quota)
    test = select_tasks(load_dataset(held_out.dataset, "test"), None, held_out.seed, held_out.quota)
    assert not {t.id for t in dev} & {t.id for t in test}


@pytest.mark.parametrize("name", SUITES[:3])
def test_a_suite_keeps_the_money_inside_the_roadmaps_stage_budgets(name: str) -> None:
    cfg = _bench_config(name)
    budgets = {"ablation": 6.0, "latency": 6.0, "headline": 11.0}
    assert cfg.max_usd <= budgets[name]
    assert cfg.spend_stop_usd == 14.5  # $0.50 of the $15 v0.3.0 budget stays in reserve
    tuning = _bench_config("ablation").max_usd + _bench_config("latency").max_usd
    assert tuning <= 6.0 and tuning + _bench_config("headline").max_usd <= 18.0


def test_a_suite_file_can_be_given_by_path_and_a_wrong_name_lists_the_real_ones(
    tmp_path: Path,
) -> None:
    mine = tmp_path / "mine.yaml"
    mine.write_text(
        "description: x\nstage: s\ndataset: toy\narms: [{name: a, strategy: solo-haiku}]\n"
    )
    assert load_suite(str(mine)).arms == ["a"]
    with pytest.raises(ConfigError, match="ablation-v3, ablation, headline-v3, headline"):
        load_suite("nope")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just a list\n")
    with pytest.raises(ConfigError, match="'arms' list"):
        load_suite(str(bad))


# ------------------------------------------------------------------------- the v3 suites (v2 data)


def test_the_v3_suites_run_on_dataset_v2() -> None:
    for name in V3:
        assert load_suite(name).config["dataset"] == "v2"


def test_ablation_v3_has_every_comparison_the_roadmap_lists() -> None:
    cfg = _bench_config("ablation-v3")
    book = arm_book(load_strategy_book(), cfg.arms)
    arms = [a.name for a in cfg.arms]
    assert arms[0] == "nano"  # the baseline: every other arm changes one thing from it
    strategies = {a.name: a.strategy for a in cfg.arms}
    # nano vs open vs mixed vs mid panels
    assert [strategies[a] for a in ("nano", "open", "mixed", "mid")] == [
        "panel-nano",
        "panel-open",
        "panel-mixed",
        "panel-mid",
    ]
    assert book.get("lenses-off").lenses == "off" and book.get("nano").lenses != "off"
    # llm (nano) vs select vs vote vs digest
    assert book.get("nano").aggregator == "llm"
    assert [book.get(a).aggregator for a in ("select", "vote", "digest")] == [
        "select",
        "vote",
        "digest",
    ]
    thresholds = [book.get(a).cascade.agreement_threshold for a in arms if a.startswith("cascade-")]
    assert min(thresholds) == 0.5 and max(thresholds) == 0.9  # the 0.5-0.9 sweep
    assert book.get("verified-synth").aggregator == "verified-synth"
    assert (
        strategies["auto-route"] == "auto" and strategies["auto-route-frontier"] == "auto-frontier"
    )
    assert cfg.split == "dev"


def test_headline_v3_is_on_the_held_out_split_and_latency_v3_has_thirty_tasks() -> None:
    headline = _bench_config("headline-v3")
    assert headline.split == "test"
    assert [a.name for a in headline.arms][:4] == [
        "solo-opus",
        "solo-sol",
        "solo-haiku",
        "solo-luna",
    ]
    latency = _bench_config("latency-v3")
    assert latency.cache is False and not latency.judge_models
    chosen = select_tasks(load_dataset(latency.dataset, "dev"), None, latency.seed, latency.quota)
    assert len(chosen) >= 30


@pytest.mark.parametrize("name", ["ablation-v3", "headline-v3"])
def test_the_v3_studies_are_judged_by_neutral_judges(name: str, tmp_path: Path) -> None:
    cfg = _bench_config(name)
    assert cfg.neutral_judges and cfg.judge_models
    catalog = load_catalog()
    env = build_env(cfg.model_copy(update={"mock": True}), [], results_dir=tmp_path)
    book = arm_book(env.book, cfg.arms)
    routing = RoutingPolicy(env.routing_config, registry=env.registry, strategies=book)
    arm_vendors = {
        catalog.models[m].vendor for a in cfg.arms for m in arm_aliases(book.get(a.name), routing)
    }
    judge_vendors = {catalog.models[j].vendor for j in cfg.judge_models}
    assert judge_vendors and not judge_vendors & arm_vendors
    assert all(catalog.models[j].provider == "openrouter" for j in cfg.judge_models)


def test_the_v3_suites_fit_task_16s_budget() -> None:
    # Task 16 has what the $15 leaves after Tasks 1-15. What bounds it is each suite's stop at the
    # ledger's $14.50 (the $0.50 reserve); the caps together stay inside that line as well.
    caps = [_bench_config(name).max_usd for name in (*V3, "route-v3")]
    assert sum(caps) <= 14.5
    for name in (*V3, "route-v3"):
        assert _bench_config(name).spend_stop_usd == 14.5


def test_route_v3_runs_each_rung_of_the_auto_ladder_alone_on_dev() -> None:
    """The threshold calibration replays the ladder over these arms, so they must be its rungs."""
    from fusion.config.loader import load_routing_policies

    cfg = _bench_config("route-v3")
    rungs = load_routing_policies().routing.escalation.rungs
    climbable = [r.strategy for r in rungs if not r.frontier]
    assert [a.strategy for a in cfg.arms] == climbable
    assert [a.name for a in cfg.arms] == [f"rung{i}" for i in range(len(climbable))]
    assert cfg.split == "dev" and cfg.neutral_judges and cfg.spend_stop_usd == 14.5


# ------------------------------------------------------------------------------- the planner


@pytest.mark.parametrize("name", SUITES)
def test_the_planner_says_each_suite_fits_its_budget(fusion_home: Path, name: str) -> None:
    result = runner.invoke(app, ["bench", "plan", "--suite", name])
    assert result.exit_code == 0, result.output
    assert "The plan fits" in result.output


def test_an_option_on_the_command_line_beats_the_suite(fusion_home: Path) -> None:
    result = runner.invoke(
        app, ["bench", "plan", "--suite", "headline", "--repeats", "1", "--arms", "solo-haiku"]
    )
    assert result.exit_code == 0 and "1 repeats" in result.output.replace("repeat ", "repeats ")
    assert "solo-opus" not in result.output


# ---------------------------------------------------------------------------------- the CLI


def test_suite_list_and_show_describe_what_would_run(fusion_home: Path) -> None:
    listed = runner.invoke(app, ["bench", "suite", "list"])
    assert listed.exit_code == 0
    assert all(name in listed.output for name in SUITES) and "tune on dev" in listed.output
    shown = runner.invoke(app, ["bench", "suite", "show", "ablation"])
    assert shown.exit_code == 0, shown.output
    assert "size-5" in shown.output and "cascade-0.5" in shown.output
    assert "stop at $14.5 cumulative" in shown.output.replace("\n", " ")
    assert runner.invoke(app, ["bench", "suite", "show", "nope"]).exit_code == 1


@pytest.mark.parametrize("name", SUITES)
def test_a_dry_run_of_each_suite_works_on_simulated_models(fusion_home: Path, name: str) -> None:
    result = runner.invoke(
        app,
        ["bench", "run", "--suite", name, "--mock", "--limit", "4", "--repeats", "1", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "completed" in result.output and "0 errors" in result.output
    runs = list((fusion_home / "project" / "bench-results").glob("bench-*"))
    assert len(runs) == 1 and (runs[0] / "meta.json").is_file()
    assert not (fusion_home / "project" / "bench-results" / "spend.json").exists()
    meta = json.loads((runs[0] / "meta.json").read_text())
    assert [a["name"] for a in meta["arms"]] == load_suite(name).arms


def test_the_whole_latency_suite_runs_with_its_own_quota(fusion_home: Path) -> None:
    result = runner.invoke(app, ["bench", "run", "--suite", "latency", "--mock", "--yes"])
    assert result.exit_code == 0, result.output
    assert "54/54" in result.output  # 9 tasks x 6 arms x 1 repeat


# ------------------------------------------------------------------------------- quotas


def _tasks() -> list[BenchTask]:
    rows = []
    for category, count in (("code_review", 6), ("debugging", 4), ("planning", 2)):
        for n in range(count):
            rows.append(
                BenchTask(
                    id=f"{category}-{n}", category=category, prompt=f"p {category} {n}", truth={}
                )  # type: ignore[arg-type]
            )
    return rows


def test_a_quota_takes_that_many_of_each_named_category_and_drops_the_rest() -> None:
    chosen = select_tasks(_tasks(), None, 0, {"code_review": 3, "planning": 2})
    assert Counter(t.category for t in chosen) == {"code_review": 3, "planning": 2}


def test_a_quota_is_seeded_and_keeps_dataset_order() -> None:
    a = select_tasks(_tasks(), None, 5, {"code_review": 3, "debugging": 2})
    b = select_tasks(_tasks(), None, 5, {"code_review": 3, "debugging": 2})
    c = select_tasks(_tasks(), None, 6, {"code_review": 3, "debugging": 2})
    assert [t.id for t in a] == [t.id for t in b] != [t.id for t in c]
    order = [t.id for t in _tasks()]
    assert [t.id for t in a] == sorted((t.id for t in a), key=order.index)


def test_a_quota_larger_than_the_category_takes_what_there_is() -> None:
    chosen = select_tasks(_tasks(), None, 0, {"planning": 9})
    assert len(chosen) == 2


def test_limit_thins_the_quota_evenly_and_no_quota_changes_nothing() -> None:
    thin = select_tasks(_tasks(), 4, 0, {"code_review": 6, "debugging": 4})
    assert len(thin) == 4 and Counter(t.category for t in thin) == {
        "code_review": 2,
        "debugging": 2,
    }
    assert [t.id for t in select_tasks(_tasks(), 5, 3)] == [
        t.id for t in select_tasks(_tasks(), 5, 3, None)
    ]
    assert len(select_tasks(_tasks(), None, 0, {})) == 12


def test_a_config_rejects_a_quota_it_cannot_use(tmp_path: Path) -> None:
    base: dict[str, Any] = {"dataset": tmp_path, "arms": [{"name": "a", "strategy": "solo-haiku"}]}
    with pytest.raises(ValueError, match="at least 1"):
        BenchConfig(**base, max_usd=1, quota={"debugging": 0})
    with pytest.raises(ValueError, match="quota"):
        BenchConfig(**base, max_usd=1, quota={"poetry": 3})


def test_a_run_uses_the_quota_tasks_and_records_them(tmp_path: Path) -> None:
    data = write_dataset(tmp_path / "data.jsonl", count=8)
    cfg = config(data, arms="solo-haiku", repeats=1, quota={"code_review": 3})
    run = run_virtual(run_bench(cfg, env=sim_env(cfg, tmp_path / "r"), run_id="q1"))
    assert run.total_jobs == 3 and len({i.task_id for i in run.items}) == 3


# --------------------------------------------------------------------------- the stop line


def test_the_stop_line_keeps_a_reserve_below_the_ledgers_cap(tmp_path: Path) -> None:
    big = write_dataset(tmp_path / "big.jsonl", count=12)
    cfg = config(big, arms="panel-nano", repeats=1, concurrency=1, mock=False)
    total = run_virtual(run_bench(cfg, env=sim_env(cfg, tmp_path / "ref"), run_id="ref")).spent_usd
    ledger = SpendLedger(tmp_path / "live" / "spend.json", cap_usd=total * 10)  # plenty
    stopped_cfg = cfg.model_copy(update={"spend_stop_usd": total / 2})
    env = sim_env(stopped_cfg, tmp_path / "live", spend=ledger)
    run = run_virtual(run_bench(stopped_cfg, env=env, run_id="s1"))
    assert run.status == "stopped" and "live-spend cap" in (run.stop_reason or "")
    assert 0 < ledger.total() <= total / 2 + 1e-9  # it stopped at the line, not at the cap


def test_the_stop_line_counts_what_earlier_runs_spent(tmp_path: Path) -> None:
    data = write_dataset(tmp_path / "data.jsonl", count=4)
    cfg = config(data, arms="solo-haiku", repeats=1, mock=False, spend_stop_usd=5.0)
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=20.0)
    ledger.append(4.9999, task="earlier", purpose="a previous run")
    env = sim_env(cfg, tmp_path / "r", spend=ledger)
    run = run_virtual(run_bench(cfg, env=env, run_id="s2"))
    assert run.done_jobs == 0 and run.status == "stopped"


def test_the_stop_line_keeps_the_ledgers_budget(tmp_path: Path) -> None:
    """The runner re-creates the ledger with the lower cap; that must not switch budgets."""
    data = write_dataset(tmp_path / "data.jsonl", count=4)
    cfg = config(data, arms="solo-haiku", repeats=1, mock=False, spend_stop_usd=5.0)
    ledger = SpendLedger(tmp_path / "spend.json", budget="v0.2.0")
    ledger.append(4.9999, task="earlier", purpose="a previous run")
    run = run_virtual(run_bench(cfg, env=sim_env(cfg, tmp_path / "r", spend=ledger), run_id="s3"))
    assert run.done_jobs == 0 and run.status == "stopped"


def test_the_plan_budget_is_what_is_left_below_the_stop_line(fusion_home: Path) -> None:
    ledger = SpendLedger(fusion_home / "project" / "bench-results" / "spend.json")
    ledger.append(14.0, task="earlier", purpose="a previous run")
    result = runner.invoke(app, ["bench", "plan", "--suite", "headline"])
    assert result.exit_code == 2 and "does not fit" in result.output  # $0.50 left under $14.50


# ---------------------------------------------------------------- per-strategy fan-out controls


def _stage(base: FanoutConfig, strategy: Any) -> PanelStage:
    stage = object.__new__(PanelStage)
    stage.deps = SimpleNamespace(routing=SimpleNamespace(budgets=SimpleNamespace(fanout=base)))  # type: ignore[attr-defined]
    stage._state = SimpleNamespace(strategy=strategy, lenses={})  # type: ignore[attr-defined]
    return stage


def test_a_strategys_fanout_controls_sit_on_top_of_the_routing_policy() -> None:
    base = FanoutConfig(max_concurrency=3, hedge_after_ms=9000)
    book = load_strategy_book()
    plain = book.get("panel-nano")
    early = plain.model_copy(
        update={"fanout": FanoutOverride(early_return=EarlyReturn(quorum=3, grace_ms=250))}
    )
    stage = _stage(base, plain)
    assert stage._fanout_config(stage._state) is base  # type: ignore[attr-defined]
    merged = _stage(base, early)._fanout_config(SimpleNamespace(strategy=early, lenses={}))  # type: ignore[arg-type]
    assert merged.early_return == EarlyReturn(quorum=3, grace_ms=250)
    assert merged.max_concurrency == 3 and merged.hedge_after_ms == 9000  # untouched fields stay
    assert base.early_return is None  # the shared policy was not changed


def test_unknown_fanout_keys_in_a_strategy_are_rejected() -> None:
    from fusion.orchestration.strategy import Strategy

    with pytest.raises(ValueError, match="max_concurrency"):
        Strategy.model_validate(
            {
                "name": "x",
                "kind": "solo",
                "members": [{"model": "claude-haiku"}],
                "fanout": {"max_concurrency": 2},
            }
        )
