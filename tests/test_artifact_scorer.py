"""``ArtifactScorer`` and a study over frontend and performance tasks: patch in, evidence out."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from _agentic import agent_env, call, submit
from _artifacts import dataset_of, flaw_patch, offline_kit, shipped, solution_patch
from _bench import config, sim_env
from fusion.bench.cli import bench_app
from fusion.bench.evaluators.base import Evidence
from fusion.bench.runner import run_bench
from fusion.bench.scoring import SCORERS, AnswerView, ScoreEnv, get_scorer, is_solved
from fusion.bench.scoring.artifact import ArtifactScorer, answer_tree, to_score_evidence, workspace
from fusion.bench.spec import BenchTask
from fusion.bench.store import BenchStore
from fusion.bench.virtual import run_virtual
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.routing.model_registry import ModelRegistry
from fusion.telemetry.cost import PricingRegistry

WIDE = {"COLUMNS": "200"}  # rich shortens column titles in a narrow terminal
PAGE = "fe-landing-page"
FAST = "perf-unique-order"


def env_without_judges(kit=None) -> ScoreEnv:
    catalog = ModelRegistry.for_mode(use_mock=False).models
    gateway = CallGateway(
        ledger=RunLedger(lambda: 0.0),
        models=catalog,
        providers={},
        pricing=PricingRegistry(),
        truncate_prompts=False,
    )
    return ScoreEnv(gateway=gateway, evaluators=kit or offline_kit())


def answer(patch: str) -> AnswerView:
    return AnswerView(final_answer=json.dumps({"patch": patch}), structured={"patch": patch})


async def test_the_reference_solution_of_a_page_completes_it() -> None:
    task = shipped(PAGE)
    result = await ArtifactScorer().score(task, answer(solution_patch(task)), env_without_judges())
    assert (
        result.scorer == "artifact"
        and result.quality >= 0.95
        and is_solved("frontend", result.quality)
    )
    assert {g["id"]: g["passed"] for g in result.details["gates"]} == {
        "tests-pass": True,
        "works-offline": True,
        "no-console-errors": None,  # no browser: unverified
    }
    kinds = {e["kind"] for e in result.evidence}
    assert {"tests", "build", "static", "diff_stats", "a11y", "screenshot", "console"} == kinds
    assert [e["id"] for e in result.evidence] == [f"E{i}" for i in range(1, len(kinds) + 1)]
    assert result.eval_seconds > 0 and result.cost_usd == 0.0 and result.trail == []
    # Criteria only a judge or a browser can score are left out, not counted as zero.
    assert set(result.details["unscored"]) == {"responsiveness", "design_fidelity", "ux_polish"}


async def test_a_flawed_page_fails_its_gate_and_completes_nothing() -> None:
    task = shipped(PAGE)
    result = await ArtifactScorer().score(task, answer(flaw_patch(task, 1)), env_without_judges())
    assert result.quality == 0.0
    failed = [g for g in result.details["gates"] if g["passed"] is False]
    assert [g["id"] for g in failed] == [
        "tests-pass"
    ] and "test_layout_adapts_to_small_screens" in failed[0]["detail"]


async def test_a_page_that_passes_its_tests_but_is_worse_scores_lower_not_zero() -> None:
    task = shipped(PAGE)
    best = await ArtifactScorer().score(task, answer(solution_patch(task)), env_without_judges())
    worse = await ArtifactScorer().score(task, answer(flaw_patch(task, 2)), env_without_judges())
    assert 0.0 < worse.quality < best.quality - 0.04


async def test_a_slow_function_fails_the_benchmark_gate_even_though_it_is_correct() -> None:
    task = shipped(FAST)
    scorer = ArtifactScorer()
    good = await scorer.score(task, answer(solution_patch(task)), env_without_judges())
    slow = await scorer.score(task, answer(flaw_patch(task, 1)), env_without_judges())
    assert good.quality >= 0.95
    assert slow.quality == 0.0
    gates = {g["id"]: g["passed"] for g in slow.details["gates"]}
    assert gates == {"tests-pass": True, "fast-enough": False}  # right answers, too slowly
    perf = next(e for e in slow.evidence if e["kind"] == "perf")
    assert perf["metrics"]["ratio_vs_reference"] > 3 and perf["metrics"]["scaling_excess"] > 0.5


async def test_an_answer_with_no_patch_or_a_patch_that_does_not_apply_scores_zero() -> None:
    task = shipped(PAGE)
    nothing = await ArtifactScorer().score(
        task, AnswerView(final_answer="I think it is fine."), env_without_judges()
    )
    assert nothing.quality == 0.0 and nothing.details["patch"] == "none"
    bad = "--- a/nope.html\n+++ b/nope.html\n@@ -1 +1 @@\n-x\n+y\n"
    rejected = await ArtifactScorer().score(task, answer(bad), env_without_judges())
    assert rejected.quality == 0.0 and rejected.details["patch"] == "rejected"
    hostile = "=== ../../escape.html ===\n<p>x</p>\n"
    assert (
        await ArtifactScorer().score(task, answer(hostile), env_without_judges())
    ).quality == 0.0


async def test_a_points_format_task_still_scores_on_its_points() -> None:
    task = BenchTask.model_validate(
        {
            "id": "p",
            "category": "frontend",
            "prompt": "Describe the layout.",
            "truth": {"points": [{"id": "a", "keywords": ["grid"]}], "decoys": []},
        }
    )
    result = await ArtifactScorer().score(
        task, AnswerView(final_answer="Use a grid."), env_without_judges()
    )
    assert result.quality == 1.0


def test_frontend_and_performance_are_scored_by_the_artifact_scorer() -> None:
    assert isinstance(SCORERS["frontend"], ArtifactScorer) and isinstance(
        SCORERS["performance"], ArtifactScorer
    )
    assert get_scorer("coding").name == "tests"


def test_the_planner_prices_the_judges_steps_only_when_judges_are_named() -> None:
    task = shipped(PAGE)
    scorer = ArtifactScorer()
    assert scorer.estimate_calls(task, []) == []
    calls = scorer.estimate_calls(task, ["claude-haiku", "gemini-flash"])
    assert len(calls) == 16 and {c.alias for c in calls} == {"claude-haiku", "gemini-flash"}
    assert calls[-1].input_tokens > calls[0].input_tokens  # the conversation grows with each step


# -- with judges ---------------------------------------------------------------------------------


async def test_judges_score_the_criteria_and_their_trail_is_stored() -> None:
    task = shipped(PAGE)

    def policy(request, step):
        return [
            call("get_evidence", label="A", kind="tests"),
            submit(
                {
                    "correctness": 1.0,
                    "accessibility": 1.0,
                    "design_fidelity": 0.5,
                    "ux_polish": 0.5,
                    "code_quality": 1.0,
                    "responsiveness": 1.0,
                }
            ),
        ][step]

    env, _ = agent_env({"claude-haiku": policy, "gemini-flash": policy})
    env.evaluators = offline_kit()
    result = await ArtifactScorer().score(task, answer(solution_patch(task)), env)
    judged = {c["id"]: c["basis"] for c in result.details["criteria"]}
    assert judged["design_fidelity"] == "judge" and "unscored" not in result.details
    assert 0.85 < result.quality < 1.0  # the judges' half marks for design pull it down
    assert result.cost_usd == pytest.approx(4 * 0.001) and len(result.trail) == 4
    assert result.details["judge"]["ended"] == ["verdict", "verdict"]
    assert result.trail[0]["tool"] == "get_evidence" and result.trail[0]["evidence_ids"] == ["E1"]


async def test_judges_that_share_a_provider_with_the_arm_are_skipped_with_a_note() -> None:
    task = shipped(PAGE)
    env, provider = agent_env(
        {"claude-haiku": lambda r, i: submit({}), "gemini-flash": lambda r, i: submit({})}
    )
    env.evaluators = offline_kit()
    env.exclude_providers = {"anthropic"}
    result = await ArtifactScorer().score(task, answer(solution_patch(task)), env)
    assert provider.requests == []
    assert (
        "judge skipped" in result.details["notes"][0] and "2 needed" in result.details["notes"][0]
    )
    assert result.quality >= 0.95  # the measured score stands


async def test_a_judge_that_never_decides_leaves_the_measured_score() -> None:
    task = shipped(PAGE)
    env, _ = agent_env(
        {j: (lambda r, i: call("list_files", label="A")) for j in ("claude-haiku", "gemini-flash")}
    )
    env.evaluators = offline_kit()
    result = await ArtifactScorer().score(task, answer(solution_patch(task)), env)
    assert result.quality >= 0.95
    assert "no judge reached a verdict" in result.details["notes"][0]
    assert result.details["judge"]["ended"] == ["step_cap", "step_cap"]


# -- evidence for the other scorers --------------------------------------------------------------


def test_evaluator_evidence_converts_for_scorers_that_take_the_older_form() -> None:
    items = [
        Evidence(kind="tests", ok=True, summary="all pass", id="E1", name="tests"),
        Evidence(
            kind="perf",
            ok=False,
            metrics={"ratio_vs_reference": 9.0},
            summary="slow",
            id="E2",
            name="perf",
        ),
    ]
    converted = to_score_evidence(items)
    assert [i.passed for i in converted.items] == [True, False]
    assert converted.items[1].value == 9.0 and "slow" in converted.render()


def test_answer_trees_apply_the_patch_to_the_tasks_files() -> None:
    task = shipped(FAST)
    tree = answer_tree(task, solution_patch(task))
    assert "fromkeys" in tree["unique.py"] and "tests/test_visible.py" in tree
    assert answer_tree(task, None) == dict(task.files)
    with workspace(tree) as root:
        assert (root / "unique.py").read_text() == tree["unique.py"]
    assert not root.exists()  # the directory goes with the block


# -- a whole study ---------------------------------------------------------------------------------


@pytest.fixture
def study(tmp_path: Path):
    dataset = dataset_of(tmp_path, PAGE, FAST)
    cfg = config(dataset, arms="solo-cheap,panel-cheap", repeats=1, limit=2)
    env = sim_env(cfg, tmp_path / "results")
    run = run_virtual(run_bench(cfg, env=env, run_id="art"))
    return cfg, env, run, tmp_path / "results"


def test_a_mock_run_scores_a_frontend_and_a_performance_task_with_evidence_attached(study) -> None:
    cfg, env, run, root = study
    assert run.status == "completed" and run.failed_jobs == 0
    assert {i.category for i in run.items} == {"frontend", "performance"}
    for item in run.items:
        assert item.score is not None and item.score.scorer == "artifact"
        kinds = {e["kind"] for e in item.score.evidence}
        assert {"tests", "static"} <= kinds
        assert ("perf" in kinds) == (item.category == "performance")
        assert item.metrics.eval_seconds == item.score.eval_seconds
        assert item.metrics.eval_seconds >= 0.0 and item.metrics.seconds_to_complete > 0
    # The evidence survives the round trip through the stored results.
    again = BenchStore(root).items("art")
    assert all(i.score and i.score.evidence for i in again)


def test_measuring_is_never_added_to_the_arms_time_or_cost(study) -> None:
    _, _, run, _ = study
    solo = [i for i in run.items if i.arm == "solo-cheap"]
    for item in solo:
        # One simulated call is all the arm took; the evaluators' wall time is a separate number.
        assert item.metrics.calls == 1 and item.metrics.seconds_to_complete < 30
        assert item.metrics.cost_usd == pytest.approx(sum(c.cost_usd or 0 for c in item.calls))
        assert item.metrics.eval_cost_usd == 0.0


def test_the_report_shows_evidence_gates_and_the_price_of_measuring(
    study, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, env, run, root = study
    monkeypatch.setenv("FUSION_BENCH_DIR", str(root))
    shown = CliRunner().invoke(bench_app, ["show", "art"], env=WIDE)
    assert shown.exit_code == 0 and "Eval $" in shown.output and "Eval s" in shown.output
    detail = CliRunner().invoke(bench_app, ["show", "art", "--task", PAGE], env=WIDE)
    assert detail.exit_code == 0, detail.output
    assert "gate tests-pass" in detail.output and "criterion correctness" in detail.output
    assert "[E1] tests" in detail.output and "not part of the arm's cost or time" in detail.output
    perf = CliRunner().invoke(bench_app, ["show", "art", "--task", FAST], env=WIDE)
    assert "[E5] perf" in perf.output or "perf " in perf.output
    missing = CliRunner().invoke(bench_app, ["show", "art", "--task", "nope"])
    assert missing.exit_code != 0


def test_a_study_with_agentic_judges_stores_the_trail_and_reports_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = dataset_of(tmp_path, PAGE)
    root = tmp_path / "results"
    cfg = config(
        dataset,
        arms="solo-cheap",
        repeats=1,
        judge_models=["gemini-flash", "gpt-sol", "claude-sonnet"],
    )
    # solo-cheap is served by one provider; the judges from the others remain.
    run = run_virtual(run_bench(cfg, env=sim_env(cfg, root), run_id="judged"))
    item = run.items[0]
    assert (
        item.score is not None
        and item.score.trail
        and item.score.details["judge"]["ended"] == ["verdict", "verdict"]
    )
    assert item.metrics.eval_cost_usd > 0 and item.metrics.eval_cost_usd == pytest.approx(
        item.score.cost_usd
    )
    monkeypatch.setenv("FUSION_BENCH_DIR", str(root))
    shown = CliRunner().invoke(bench_app, ["show", "judged", "--json"])
    gate = json.loads(shown.output)["judge_gate"]
    assert gate["blocked"] is True and "has not been calibrated" in gate["reasons"][0]
