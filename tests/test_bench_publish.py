"""From a dev study to a default, and from runs to the published results document."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from _bench import config
from _stats import item, two_arms
from fusion.bench.meta import ArmMeta
from fusion.bench.recommend import recommend
from fusion.bench.report import build_report
from fusion.bench.report.results import LIMITATIONS, render_results, spend_by_run
from fusion.bench.stats import Rules, study_stats
from fusion.bench.store import BenchRunRecord
from fusion.cli.app import app

runner = CliRunner()
FAST = Rules(n_boot=200)


def _arms(spec: dict[str, tuple[float, float]], tasks: int = 12) -> list[Any]:
    """Arms with a fixed quality and cost per task: ``{arm: (quality, cost)}``."""
    return [
        item(arm, f"t{n}", quality, cost=cost, solved=quality >= 0.6)
        for arm, (quality, cost) in spec.items()
        for n in range(tasks)
    ]


def _advice(spec: dict[str, tuple[float, float]], **kw: Any) -> Any:
    return recommend(study_stats(_arms(spec), rules=FAST).arms, **kw)


# ------------------------------------------------------------------------------ recommend


def test_it_takes_the_cheapest_frontier_arm_within_the_margin_of_the_best() -> None:
    advice = _advice(
        {
            "big": (0.80, 0.05),  # best, dearest
            "mid": (0.78, 0.02),  # within 0.03 of the best and much cheaper: the pick
            "cheap": (0.60, 0.01),  # on the frontier but too far below the best
            "dominated": (0.70, 0.04),  # dearer and worse than mid
        }
    )
    assert advice.chosen == "mid" and advice.best_quality_arm == "big"
    by_arm = {c.arm: c for c in advice.candidates}
    assert by_arm["cheap"].on_frontier and not by_arm["cheap"].within_margin
    assert not by_arm["dominated"].on_frontier
    assert "cheapest per solved task" in advice.reason and "big" in advice.reason


def test_the_best_arm_is_chosen_when_nothing_cheaper_is_close() -> None:
    advice = _advice({"big": (0.9, 0.05), "cheap": (0.5, 0.01)})
    assert advice.chosen == "big"


def test_the_margin_decides_how_much_quality_may_be_given_up() -> None:
    spec = {"big": (0.80, 0.05), "mid": (0.74, 0.02)}
    assert _advice(spec, margin=0.03).chosen == "big"
    assert _advice(spec, margin=0.10).chosen == "mid"


def test_it_says_when_the_choice_is_a_lean_not_a_finding() -> None:
    items = two_arms(12, effect=0.0, noise=0.2, repeats=1)
    advice = recommend(study_stats(items, rules=FAST).arms, margin=0.5)
    assert advice.chosen is not None
    if advice.chosen != advice.best_quality_arm:
        assert advice.noisy and "overlaps" in advice.reason


def test_only_the_candidates_asked_for_are_considered() -> None:
    spec = {"big": (0.80, 0.05), "mid": (0.79, 0.02), "other": (0.79, 0.001)}
    assert _advice(spec, among=["big", "mid"]).chosen == "mid"


def test_arms_that_solved_nothing_cannot_be_chosen_for_their_price() -> None:
    advice = _advice({"a": (0.2, 0.01), "b": (0.3, 0.02)})
    assert advice.chosen is None and "cost per solved" in advice.reason


def test_an_empty_pool_is_an_answer_not_a_crash() -> None:
    advice = _advice({"a": (0.8, 0.01)}, among=["nope"])
    assert advice.chosen is None and "no candidate" in advice.reason


def test_the_chosen_arm_comes_with_the_suite_line_that_runs_it() -> None:
    arm = ArmMeta(
        name="size-2",
        strategy="panel-cheap",
        overrides={
            "members": [{"model": "claude-haiku"}, {"model": "gpt-luna", "temperature": 0.5}]
        },
    )
    advice = _advice({"size-2": (0.8, 0.01), "x": (0.5, 0.02)}, arm_meta=[arm])
    assert advice.strategy == "panel-cheap" and advice.overrides == arm.overrides
    assert advice.arm_line() == (
        "  - {name: fusion-best, strategy: panel-cheap, overrides: "
        "{members: [{model: claude-haiku}, {model: gpt-luna, temperature: 0.5}]}}"
    )
    plain = recommend(
        study_stats(_arms({"panel-cascade": (0.8, 0.01)}), rules=FAST).arms,
        arm_meta=[ArmMeta(name="panel-cascade", strategy="panel-cascade")],
    )
    assert plain.arm_line("best") == "  - {name: best, strategy: panel-cascade}"
    assert _advice({"size-2": (0.8, 0.01)}).arm_line() is None  # no record of how it was built


# --------------------------------------------------------------------------------- results


def _record(run_id: str = "bench-20261006-120000-abcd", mock: bool = False) -> BenchRunRecord:
    return BenchRunRecord(
        run_id=run_id,
        status="completed",
        dataset="v1",
        mock=mock,
        config=config(Path("v1"), arms="solo-cheap", repeats=2, mock=mock),
        total_jobs=10,
        done_jobs=10,
        spent_usd=3.0,
        eval_spent_usd=0.2,
        created_at="2026-10-06 12:00:00",
        updated_at="2026-10-06 12:30:00",
    )


def test_the_results_document_carries_the_verdict_text_verbatim() -> None:
    report = build_report(
        _record(), two_arms(14, effect=0.1, noise=0.05), baseline="solo", rules=FAST
    )
    text = render_results(report, today="2026-10-06")
    assert text.startswith("# Benchmark results")
    assert "Generated by `fusion bench results bench-20261006-120000-abcd`" in text
    for cmp in report.stats.comparisons:
        for verdict in cmp.verdicts:
            assert verdict.reason in text  # nothing paraphrased
    assert "## Headline" in text and "### Efficiency and speed" in text
    assert "## Methodology" in text and "## Limitations" in text and "## Reproduce" in text
    assert "```bash" in text and "fusion bench results bench-20261006-120000-abcd" in text


def test_the_limitations_cannot_be_lost_and_say_what_a_reader_must_know() -> None:
    text = render_results(
        build_report(_record(), two_arms(12), baseline="solo", rules=FAST), today="2026-10-06"
    )
    assert LIMITATIONS in text
    for fact in ("synthetic", "inconclusive", "judge", "prices", "held-out"):
        assert fact in LIMITATIONS


def test_a_simulated_run_is_never_presented_as_a_measurement() -> None:
    report = build_report(_record(mock=True), two_arms(12), baseline="solo", rules=FAST)
    assert "not measurements of real models" in render_results(report, today="2026-10-06")


def test_supporting_runs_follow_the_headline_one_level_down() -> None:
    head = build_report(
        _record("bench-20261006-120000-aaaa"), two_arms(12), baseline="solo", rules=FAST
    )
    side = build_report(
        _record("bench-20261006-120000-bbbb"), two_arms(12), baseline="solo", rules=FAST
    )
    text = render_results(head, [side], today="2026-10-06")
    assert "## Supporting study: bench-20261006-120000-bbbb" in text
    after = text[text.index("## Supporting study") :]
    assert "### Headline" in after and "#### Arms" not in after.split("### Headline")[0]
    assert "\n# " not in text[2:]  # one title only


def test_the_spend_table_lists_each_run_and_the_total_against_the_cap() -> None:
    ledger = [
        {"task": "bench", "purpose": "bench-20261006-120000-aaaa solo-a t1", "usd": 1.5},
        {"task": "bench", "purpose": "bench-20261006-120000-aaaa solo-a t2", "usd": 0.5},
        {"task": "bench", "purpose": "bench-20261006-130000-bbbb solo-a t1", "usd": 1.0},
        {"task": "judge-calibration", "purpose": "calibration", "usd": 0.25},
    ]
    assert spend_by_run(ledger) == [
        ("bench-20261006-120000-aaaa", 2.0),
        ("bench-20261006-130000-bbbb", 1.0),
        ("judge-calibration", 0.25),
    ]
    report = build_report(_record(), two_arms(12), baseline="solo", rules=FAST)
    text = render_results(report, ledger=ledger, today="2026-10-06")
    assert "## Spend" in text and "| bench-20261006-120000-aaaa | $2.00 |" in text
    assert "**$3.25** of the $20 cap" in text
    assert "## Spend" not in render_results(report, today="2026-10-06")


def test_an_html_link_is_shown_when_there_is_a_report_file() -> None:
    report = build_report(_record(), two_arms(12), baseline="solo", rules=FAST)
    text = render_results(report, html_link="benchmark-report.html", today="2026-10-06")
    assert "[benchmark-report.html](benchmark-report.html)" in text


# ------------------------------------------------------------------------------------- cli


def _invoke(*args: str) -> Any:
    return runner.invoke(app, ["bench", *args])


@pytest.fixture
def run_id(fusion_home: Path) -> str:
    result = _invoke("run", "--suite", "ablation", "--mock", "--limit", "12", "--yes")
    assert result.exit_code == 0, result.output
    return next(p.name for p in (fusion_home / "project" / "bench-results").glob("bench-*"))


def test_recommend_prints_the_candidates_the_reason_and_the_suite_line(run_id: str) -> None:
    result = _invoke("recommend", run_id, "--margin", "0.1")
    assert result.exit_code == 0, result.output
    assert "Candidates" in result.output and "size-5" in result.output
    assert "cheapest per solved task" in result.output
    assert "{name: fusion-best, strategy: panel-cheap" in result.output


def test_recommend_json_and_among(run_id: str) -> None:
    data = json.loads(_invoke("recommend", run_id, "--json", "--among", "size-3,size-5").output)
    assert data["chosen"] in ("size-3", "size-5")
    assert {c["arm"] for c in data["candidates"]} == {"size-3", "size-5"}


def test_recommend_leaves_out_single_model_arms_by_default(run_id: str) -> None:
    data = json.loads(_invoke("recommend", run_id, "--json").output)
    assert "size-1" not in {c["arm"] for c in data["candidates"]}


def test_results_writes_the_document_and_the_html_beside_it(fusion_home: Path, run_id: str) -> None:
    out = fusion_home / "docs" / "BENCHMARK_RESULTS.md"
    html = fusion_home / "docs" / "benchmark-report.html"
    result = _invoke("results", run_id, "-o", str(out), "--html", str(html), "--resamples", "200")
    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert (
        "# Benchmark results" in text and "[benchmark-report.html](benchmark-report.html)" in text
    )
    assert "not measurements of real models" in text and "## Limitations" in text
    assert "--html" in text and "<svg" in html.read_text()
    assert "## Spend" not in text  # a simulated run records no spend


def test_results_appends_supporting_runs(fusion_home: Path, run_id: str) -> None:
    other = _invoke("run", "--suite", "latency", "--mock", "--limit", "6", "--yes")
    assert other.exit_code == 0
    ids = sorted(p.name for p in (fusion_home / "project" / "bench-results").glob("bench-*"))
    second = next(i for i in ids if i != run_id)
    out = fusion_home / "r.md"
    result = _invoke("results", run_id, "--also", second, "-o", str(out), "--resamples", "200")
    assert result.exit_code == 0, result.output
    assert f"## Supporting study: {second}" in out.read_text()
    assert f"--also {second}" in out.read_text()


def test_results_of_an_unknown_run_is_a_plain_error(fusion_home: Path) -> None:
    result = _invoke("results", "no-such-run", "-o", str(fusion_home / "x.md"))
    assert result.exit_code == 1 and "no-such-run" in result.output
