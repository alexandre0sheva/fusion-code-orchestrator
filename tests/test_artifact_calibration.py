"""Calibrating the agentic judge on known-good against known-flawed outputs, and the accuracy
floor below which a study gets no headline verdict."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from _agentic import agent_env, call, submit
from _artifacts import V1, dataset_of, offline_kit, shipped
from fusion.bench.cli import bench_app
from fusion.bench.scoring.artifact_calibration import (
    artifact_calibration_calls,
    artifact_cases,
    calibrate_artifacts,
)
from fusion.bench.scoring.calibration import (
    ACCURACY_FLOOR_ENV,
    CalibrationReport,
    JudgeStats,
    judge_floor,
    judge_gate,
    load_reports,
)
from fusion.bench.spec import load_dataset

WIDE = {"COLUMNS": "200"}
TASK = "perf-unique-order"


def stats(judge: str, accuracy: float) -> JudgeStats:
    return JudgeStats(
        judge=judge, cases=10, accuracy=accuracy, tie_rate=0.0, inconsistent_rate=0.0, kappa=0.5
    )


def report(kind: str = "artifact", mock: bool = False, **accuracy: float) -> CalibrationReport:
    return CalibrationReport(
        id=f"cal-{len(accuracy)}",
        created="2026-10-06T00:00:00Z",
        cases=10,
        judges=[stats(j, a) for j, a in accuracy.items()],
        kind=kind,
        mock=mock,  # type: ignore[arg-type]
    )


# -- the cases ---------------------------------------------------------------------------------


def test_cases_pair_every_flaw_of_every_frontend_and_performance_task() -> None:
    tasks = load_dataset(V1, "all")
    cases = artifact_cases(tasks)
    assert {c.set for c in cases} == {"visual", "perf"}
    assert (
        len([c for c in cases if c.set == "visual"]) == 24
        and len([c for c in cases if c.set == "perf"]) == 24
    )
    assert all(c.task_id.startswith(("fe-", "perf-")) for c in cases)  # no text task has any


def test_a_cap_per_set_picks_the_same_cases_every_time_and_a_different_seed_others() -> None:
    tasks = load_dataset(V1, "all")
    first = artifact_cases(tasks, per_set=5, seed=1)
    assert first == artifact_cases(tasks, per_set=5, seed=1)
    assert len(first) == 10 and first != artifact_cases(tasks, per_set=5, seed=2)


def test_the_forecast_counts_two_orderings_of_about_eight_steps_per_judge_and_case() -> None:
    tasks = [shipped(TASK)]
    cases = artifact_cases(tasks)
    calls = artifact_calibration_calls(tasks, cases, ["claude-haiku", "gemini-flash"])
    assert len(calls) == len(cases) * 2 * 2 * 8


# -- the statistics ------------------------------------------------------------------------------


def perfect(request: Any, step: int) -> dict[str, Any]:
    """Looks at both outputs' tests evidence, then names the one whose tests pass."""
    if step == 0:
        return call("get_evidence", label="A", kind="tests")
    if step == 1:
        return call("get_evidence", label="B", kind="tests")
    texts = [m.content for m in request.messages[1:] if m.role == "user"]
    first, second = texts[0], texts[1]  # the observations for A, then B
    a_pass, b_pass = "tests PASS" in first, "tests PASS" in second
    winner = "A" if a_pass and not b_pass else "B" if b_pass and not a_pass else "tie"
    return submit(
        {
            "A": {"correctness": 1.0 if a_pass else 0.0},
            "B": {"correctness": 1.0 if b_pass else 0.0},
        },
        winner=winner,
    )


def always_a(request: Any, step: int) -> dict[str, Any]:
    if step == 0:
        return call("get_evidence", label="A", kind="tests")
    return submit({"A": {"correctness": 0.5}, "B": {"correctness": 0.5}}, winner="A")


def never_decides(request: Any, step: int) -> dict[str, Any]:
    return call("list_files", label="A")


async def test_a_judge_that_reads_the_evidence_beats_one_that_follows_position() -> None:
    task = shipped(TASK)  # flaw 1 is slow but correct; flaw 2 is fast but wrong (fails tests)
    cases = [c for c in artifact_cases([task]) if c.flaw == 2]
    env, _ = agent_env({"claude-haiku": perfect, "gemini-flash": always_a})
    report = await calibrate_artifacts(
        [task], cases, env, ["claude-haiku", "gemini-flash"], offline_kit(), mock=True, floor=0.8
    )
    by_judge = {j.judge: j for j in report.judges}
    assert report.kind == "artifact" and report.cases == 1 and report.mock
    assert by_judge["claude-haiku"].accuracy == 1.0 and by_judge[
        "claude-haiku"
    ].kappa == pytest.approx(1.0)
    assert (
        by_judge["gemini-flash"].accuracy == 0.0
        and by_judge["gemini-flash"].inconsistent_rate == 1.0
    )
    assert by_judge["gemini-flash"].kappa <= 0.0  # always "A" is no better than chance
    assert report.by_set == {"perf": {"claude-haiku": 1.0, "gemini-flash": 0.0}}
    assert report.agreement == 0.0 and "claude-haiku|gemini-flash" in report.pair_kappa
    assert report.cost_usd > 0


async def test_judges_that_cannot_decide_are_counted_as_failed_calls_and_ties() -> None:
    task = shipped(TASK)
    cases = artifact_cases([task])[:1]
    env, _ = agent_env({"claude-haiku": never_decides})
    report = await calibrate_artifacts([task], cases, env, ["claude-haiku"], offline_kit())
    only = report.judges[0]
    assert only.accuracy == 0.0 and only.tie_rate == 1.0 and only.failed_calls == 2


async def test_calibration_needs_judges_and_cases() -> None:
    from fusion.bench.scoring import ScoringError

    task = shipped(TASK)
    env, _ = agent_env({"claude-haiku": perfect})
    with pytest.raises(ScoringError, match="at least one judge"):
        await calibrate_artifacts([task], artifact_cases([task]), env, [], offline_kit())
    with pytest.raises(ScoringError, match="no frontend or performance task"):
        await calibrate_artifacts([task], [], env, ["claude-haiku"], offline_kit())


# -- the accuracy floor ----------------------------------------------------------------------------


def test_a_judge_with_no_calibration_blocks_the_headline_verdict() -> None:
    gate = judge_gate(["claude-haiku"], [])
    assert gate.blocked and gate.accuracy == {"claude-haiku": None}
    assert "has not been calibrated" in gate.reasons[0]
    text_only = judge_gate(["claude-haiku"], [report(kind="text", **{"claude-haiku": 0.99})])
    assert text_only.blocked  # a text calibration says nothing about judging artifacts


def test_a_judge_below_the_floor_blocks_and_one_above_it_does_not() -> None:
    reports = [report(**{"claude-haiku": 0.9, "gemini-flash": 0.7})]
    gate = judge_gate(["claude-haiku", "gemini-flash"], reports, floor=0.8)
    assert gate.blocked and len(gate.reasons) == 1
    assert (
        "gemini-flash picked the better output 70% of the time, below the 80% floor"
        in gate.reasons[0]
    )
    assert not judge_gate(["claude-haiku"], reports, floor=0.8).blocked
    assert judge_gate(["claude-haiku"], reports, floor=0.95).blocked  # the floor is configurable
    assert judge_gate(["claude-haiku"], reports, floor=0.9).blocked is False  # equal passes


def test_the_latest_calibration_of_a_judge_is_the_one_that_counts() -> None:
    old, new = report(**{"claude-haiku": 0.5}), report(**{"claude-haiku": 0.95})
    assert not judge_gate(["claude-haiku"], [old, new], floor=0.8).blocked
    assert judge_gate(["claude-haiku"], [new, old], floor=0.8).blocked


def test_the_floor_defaults_to_eighty_percent_and_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ACCURACY_FLOOR_ENV, raising=False)
    assert judge_floor() == 0.8
    monkeypatch.setenv(ACCURACY_FLOOR_ENV, "0.65")
    assert judge_floor() == 0.65
    for junk in ("high", "1.5", "-1", ""):
        monkeypatch.setenv(ACCURACY_FLOOR_ENV, junk)
        assert judge_floor() == 0.8
    monkeypatch.setenv(ACCURACY_FLOOR_ENV, "0.99")
    assert judge_gate(["claude-haiku"], [report(**{"claude-haiku": 0.95})]).blocked


# -- the command -------------------------------------------------------------------------------


def test_the_command_calibrates_simulated_judges_and_stores_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = dataset_of(tmp_path, TASK, "fe-landing-page")
    monkeypatch.setenv("FUSION_BENCH_DIR", str(tmp_path / "results"))
    done = CliRunner().invoke(
        bench_app,
        [
            "calibrate-judge",
            "--dataset",
            str(dataset),
            "--artifacts",
            "--mock",
            "--cases-per-set",
            "2",
        ],
        env=WIDE,
    )
    assert done.exit_code == 0, done.output
    assert "perf acc." in done.output and "visual acc." in done.output
    assert "simulated judges: not a measurement" in done.output
    stored = load_reports(tmp_path / "results")
    assert (
        len(stored) == 1
        and stored[0].kind == "artifact"
        and stored[0].mock
        and stored[0].cases == 4
    )
    assert {j.judge for j in stored[0].judges}  # three simulated judges
    as_json = CliRunner().invoke(
        bench_app,
        [
            "calibrate-judge",
            "--dataset",
            str(dataset),
            "--artifacts",
            "--mock",
            "--cases-per-set",
            "1",
            "--json",
        ],
    )
    assert json.loads(as_json.output)["kind"] == "artifact"


def test_a_study_is_unblocked_once_its_judges_are_calibrated_above_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _bench import config, sim_env
    from fusion.bench.runner import run_bench
    from fusion.bench.scoring.calibration import save_report
    from fusion.bench.virtual import run_virtual

    dataset = dataset_of(tmp_path, "fe-landing-page")
    root = tmp_path / "results"
    cfg = config(dataset, arms="solo-cheap", repeats=1, judge_models=["gemini-flash", "gpt-sol"])
    run_virtual(run_bench(cfg, env=sim_env(cfg, root), run_id="r"))
    monkeypatch.setenv("FUSION_BENCH_DIR", str(root))
    blocked = json.loads(CliRunner().invoke(bench_app, ["show", "r", "--json"]).output)[
        "judge_gate"
    ]
    assert blocked["blocked"] is True
    save_report(report(mock=True, **{"gemini-flash": 0.9, "gpt-sol": 0.85}), root)
    shown = CliRunner().invoke(bench_app, ["show", "r"], env=WIDE)
    assert "Judge reliability" in shown.output and "headline verdict allowed" in shown.output
    save_report(report(mock=True, **{"gemini-flash": 0.9, "gpt-sol": 0.5}), root)
    again = CliRunner().invoke(bench_app, ["show", "r"], env=WIDE)
    assert (
        "Headline verdict blocked" in again.output
        and "gpt-sol picked the better output 50%" in again.output
    )
