"""Reports: the Markdown and HTML renderings, the methodology footer, evidence, `report` and
`compare`. Runs are simulated (``--mock``) or built by hand; nothing needs an API key."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from _bench import config, sim_env, write_dataset
from _stats import item, two_arms
from fusion.bench.meta import RunMeta, load_meta, reconstruct_meta
from fusion.bench.report import build_report, render_html, render_markdown
from fusion.bench.report.compare import compare_runs, render_comparison
from fusion.bench.report.data import screenshot_path
from fusion.bench.runner import run_bench
from fusion.bench.scoring import ScoreResult
from fusion.bench.scoring.calibration import JudgeGate
from fusion.bench.stats import Rules
from fusion.bench.store import BenchItem, BenchRunRecord, BenchStore
from fusion.bench.virtual import run_virtual
from fusion.cli.app import app

runner = CliRunner()
FAST = Rules(n_boot=300)
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _record(cfg: Any, run_id: str = "r1", *, mock: bool = True) -> BenchRunRecord:
    return BenchRunRecord(
        run_id=run_id,
        status="completed",
        dataset="data.jsonl",
        mock=mock,
        config=cfg,
        total_jobs=10,
        done_jobs=10,
        spent_usd=1.25,
        eval_spent_usd=0.5,
        created_at="2026-10-06 10:00:00",
        updated_at="2026-10-06 10:05:00",
    )


@pytest.fixture
def study(tmp_path: Path) -> tuple[BenchStore, BenchRunRecord, list[BenchItem]]:
    """A finished simulated study of three arms over twelve tasks."""
    data = write_dataset(tmp_path / "data.jsonl", count=12)
    cfg = config(data, arms="solo-cheap,panel-cheap,solo-frontier", repeats=2)
    env = sim_env(cfg, tmp_path / "results")
    run = run_virtual(run_bench(cfg, env=env, run_id="r1"))
    record = env.store.get_run("r1")
    assert record is not None and run.status == "completed"
    return env.store, record, env.store.items("r1")


# ------------------------------------------------------------------------ the snapshot


def test_a_run_writes_a_snapshot_of_what_it_was(
    study: tuple[BenchStore, BenchRunRecord, list[BenchItem]],
) -> None:
    store, record, _ = study
    meta = load_meta(store.run_dir("r1"))
    assert meta is not None and not meta.reconstructed
    assert meta.task_count == 12 and len(meta.dataset_hash) == 16
    assert [a.name for a in meta.arms] == ["solo-cheap", "panel-cheap", "solo-frontier"]
    assert all(m.verified_on for m in meta.models.values() if m.input_per_1m is not None)
    assert set(meta.tasks) == {f"t{n}" for n in range(12)}


def test_a_resumed_run_keeps_the_snapshot_of_its_start(tmp_path: Path) -> None:
    data = write_dataset(tmp_path / "data.jsonl", count=4)
    cfg = config(data, arms="solo-cheap", repeats=1)
    env = sim_env(cfg, tmp_path / "results")
    run_virtual(run_bench(cfg, env=env, run_id="r1"))
    path = env.store.run_dir("r1") / "meta.json"
    snapshot = json.loads(path.read_text())
    snapshot["created_at"] = "2020-01-01T00:00:00+00:00"  # as if taken long ago
    path.write_text(json.dumps(snapshot))
    run_virtual(run_bench(cfg, env=sim_env(cfg, tmp_path / "results"), run_id="r1"))
    assert json.loads(path.read_text())["created_at"] == "2020-01-01T00:00:00+00:00"


def test_a_run_without_a_snapshot_is_reconstructed_and_says_so(
    study: tuple[BenchStore, BenchRunRecord, list[BenchItem]],
) -> None:
    store, record, items = study
    (store.run_dir("r1") / "meta.json").unlink()
    from fusion.routing.model_registry import ModelRegistry

    meta = reconstruct_meta(record.config, ModelRegistry.for_mode(use_mock=False).models)
    assert meta is not None and meta.reconstructed
    report = build_report(record, items, meta=meta, rules=FAST)
    assert any("read from today's files" in n for n in report.notes)
    assert "read from today's files" in render_markdown(report)


def test_a_missing_dataset_leaves_the_report_without_a_snapshot_not_broken(
    study: tuple[BenchStore, BenchRunRecord, list[BenchItem]],
) -> None:
    _, record, items = study
    cfg = record.config.model_copy(update={"dataset": Path("/no/such/dataset.jsonl")})
    assert reconstruct_meta(cfg, {}) is None
    report = build_report(record, items, meta=None, rules=FAST)
    assert any("No dataset snapshot" in n for n in report.notes)
    assert report.stats.by_difficulty == {}
    assert "no snapshot of its contents" in render_markdown(report)


# --------------------------------------------------------------------------- markdown


def test_the_markdown_report_has_every_section_and_the_methodology_footer(
    study: tuple[BenchStore, BenchRunRecord, list[BenchItem]],
) -> None:
    store, record, items = study
    meta = load_meta(store.run_dir("r1"))
    assert meta is not None
    text = render_markdown(build_report(record, items, meta=meta, rules=FAST))
    for heading in (
        "# Benchmark report: r1",
        "## Headline",
        "## Arms",
        "### Efficiency and speed",
        "### Cost of measuring",
        "### Variance across repeats",
        "## Comparisons against the baseline",
        "## By category",
        "## By difficulty",
        "## Methodology",
    ):
        assert heading in text
    footer = text[text.index("## Methodology") :]
    assert meta.dataset_hash in footer
    for arm in ("solo-cheap", "panel-cheap", "solo-frontier"):
        assert f"**Arm {arm}:**" in footer
    assert "claude-haiku" in footer and "verified 2026" in footer  # model id and price date
    assert "**Repeats / seed:** 2 per task / seed 0" in footer
    assert "**Judge models:** none" in footer
    assert "**Spend:**" in footer and "bootstrap" in footer and "non-inferiority" in footer
    assert "Simulated run" in text  # the caveat leads the report


def test_the_headline_names_the_baseline_and_gives_a_reason_per_verdict(
    study: tuple[BenchStore, BenchRunRecord, list[BenchItem]],
) -> None:
    _, record, items = study
    text = render_markdown(build_report(record, items, baseline="solo-frontier", rules=FAST))
    headline = text[text.index("## Headline") : text.index("## Arms")]
    assert "solo-frontier" in headline
    assert re.search(r"panel-cheap vs solo-frontier on all \(12 tasks\)", headline)
    assert any(sym in headline for sym in ("✓", "✗", "?"))


def test_every_verdict_in_the_report_comes_with_its_reason(
    study: tuple[BenchStore, BenchRunRecord, list[BenchItem]],
) -> None:
    _, record, items = study
    report = build_report(record, items, rules=FAST)
    assert report.stats.comparisons
    for cmp in report.stats.comparisons:
        assert [v.claim for v in cmp.verdicts] == ["cheaper", "faster", "not_worse", "better"]
        assert all(v.reason for v in cmp.verdicts)


def test_eval_cost_is_shown_beside_the_arm_and_not_added_to_it() -> None:
    items = [
        item("a", f"t{n}", 0.9, cost=0.10, eval_cost_usd=0.40, eval_seconds=3.0) for n in range(12)
    ]
    cfg = config(Path("x"), arms="solo-cheap")
    report = build_report(_record(cfg), items, rules=FAST)
    arm = report.stats.arms[0]
    assert arm.cost_per_task is not None and arm.cost_per_task.estimate == pytest.approx(0.10)
    text = render_markdown(report)
    assert "### Cost of measuring" in text and "$0.4000" in text
    html = render_html(report)
    assert "Eval $/item" in html and "never inside" in html


# ------------------------------------------------------------------------------- html


def test_the_html_report_is_one_file_with_no_external_reference(
    study: tuple[BenchStore, BenchRunRecord, list[BenchItem]],
) -> None:
    store, record, items = study
    report = build_report(record, items, meta=load_meta(store.run_dir("r1")), rules=FAST)
    html = render_html(report, store.run_dir("r1"))
    assert html.startswith("<!doctype html>")
    assert "http://" not in html and "https://" not in html and "//cdn" not in html
    for forbidden in ("<link", "@import", "url(", "<iframe", "<script src", ' src="http', "href="):
        assert forbidden not in html
    assert html.count("<svg") >= 4 and "prefers-color-scheme: dark" in html
    assert 'data-theme="dark"' in html and 'id="theme"' in html
    assert "viewport" in html and 'role="img"' in html and "<title>" in html


def test_the_html_has_the_charts_and_a_table_with_the_same_numbers(
    study: tuple[BenchStore, BenchRunRecord, list[BenchItem]],
) -> None:
    _, record, items = study
    report = build_report(record, items, rules=FAST)
    html = render_html(report)
    for chart in (
        "Quality and cost",
        "Quality and speed",
        "Quality by category",
        "Where the time goes",
    ):
        assert chart in html
    for arm in report.stats.arms:
        assert html.count(f">{arm.arm}<") >= 1
        assert arm.mean_quality is not None and f"{arm.mean_quality.estimate:.3f}" in html
    assert "<table>" in html and "Frontier" in html
    assert html.count('class="pt"') >= 3 * len(report.stats.arms)  # every mark has a tooltip


def test_html_escapes_text_that_comes_from_the_data() -> None:
    cfg = config(Path("x"), arms="solo-cheap")
    items = [item("<b>arm</b>", f"t{n}", 0.8, category="frontend", solved=True) for n in range(3)]
    report = build_report(_record(cfg), items, rules=FAST)
    html = render_html(report)
    assert "<b>arm</b>" not in html and "&lt;b&gt;arm&lt;/b&gt;" in html


def test_html_has_a_colour_per_arm_in_a_fixed_order() -> None:
    cfg = config(Path("x"), arms="solo-cheap")
    items = [item(a, f"t{n}", 0.5 + i / 20) for i, a in enumerate("abc") for n in range(12)]
    html = render_html(build_report(_record(cfg), items, rules=FAST))
    legend = re.search(r'<div class="legend" aria-label="Arms">(.*?)</div>', html)
    assert legend is not None
    assert re.findall(r"bg(\d)", legend.group(1)) == ["1", "2", "3"]


# ------------------------------------------------------------------ evidence and examples


def _artifact_item(
    arm: str, task: str, quality: float, shot: Path | None, summary: str = "ok"
) -> BenchItem:
    base = item(arm, task, quality, category="frontend")
    evidence = [
        {"id": "E1", "kind": "tests", "ok": quality > 0.5, "status": "measured",
         "summary": summary, "metrics": {"passed": 3.0}},
        {"id": "E2", "kind": "screenshot", "ok": True, "status": "measured", "summary": "shot",
         "artifact_path": str(shot) if shot else None, "metrics": {}},
    ]  # fmt: skip
    base.score = ScoreResult(
        quality=quality,
        scorer="artifact",
        details={
            "gates": [{"id": "tests-pass", "passed": quality > 0.5, "detail": ""}],
            "judge": {"justification": "the page overflows on mobile"},
        },
        evidence=evidence,
    )
    return base


def test_the_most_different_pairs_are_shown_with_their_evidence_and_screenshots(
    tmp_path: Path,
) -> None:
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG)
    items = []
    for n in range(12):
        gap = 0.9 if n == 3 else 0.05
        items += [
            _artifact_item("fusion", f"t{n}", 0.5 + gap / 2, shot, "FUSION-EVIDENCE"),
            _artifact_item("solo", f"t{n}", 0.5 - gap / 2, shot, "SOLO-EVIDENCE"),
        ]
    cfg = config(Path("x"), arms="solo-cheap")
    report = build_report(_record(cfg), items, baseline="solo", rules=FAST)
    assert report.examples and report.examples[0].task_id == "t3"
    assert report.examples[0].gap == pytest.approx(0.9)
    html = render_html(report)
    assert "FUSION-EVIDENCE" in html and "SOLO-EVIDENCE" in html
    assert "data:image/png;base64," in html and "gate tests-pass" in html
    assert "the page overflows on mobile" in html
    assert "not part of the arm" in html  # eval cost and time are labelled as such
    md = render_markdown(report)
    assert "FUSION-EVIDENCE" in md and "screenshot" in md and str(shot) in md


def test_a_missing_or_oversized_screenshot_is_named_not_embedded(tmp_path: Path) -> None:
    big = tmp_path / "big.png"
    big.write_bytes(PNG + b"\x00" * 450_000)
    items = []
    for n in range(12):
        items += [
            _artifact_item("fusion", f"t{n}", 0.9, big if n == 0 else tmp_path / "gone.png"),
            _artifact_item("solo", f"t{n}", 0.2, big if n == 0 else tmp_path / "gone.png"),
        ]
    cfg = config(Path("x"), arms="solo-cheap")
    html = render_html(build_report(_record(cfg), items, baseline="solo", rules=FAST))
    assert "data:image" not in html
    assert "not embedded" in html or "not found" in html


def test_a_screenshot_is_found_under_the_run_folder_when_the_path_moved(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "evidence" / "deep").mkdir(parents=True)
    (run_dir / "evidence" / "deep" / "shot.png").write_bytes(PNG)
    assert screenshot_path(run_dir, "/old/place/shot.png") == run_dir / "evidence/deep/shot.png"
    assert screenshot_path(run_dir, "/old/place/other.png") is None


def test_evidence_is_only_taken_from_artifact_categories_with_a_baseline_to_pair() -> None:
    cfg = config(Path("x"), arms="solo-cheap")
    items = two_arms(12)  # code review: no evidence
    assert build_report(_record(cfg), items, baseline="solo", rules=FAST).examples == []


# ------------------------------------------------------------------------------ gate


def test_a_blocked_judge_gate_withholds_the_headline_and_the_judged_categories() -> None:
    items = two_arms(12, category="code_review") + [
        i.model_copy(update={"category": "frontend"}) for i in two_arms(12, prefix="f")
    ]
    for i in items:
        if i.category == "frontend":
            i.score = ScoreResult(quality=0.7, scorer="artifact", trail=[{"tool": "x"}])
    gate = JudgeGate(blocked=True, floor=0.8, reasons=["judge j has not been calibrated"])
    cfg = config(Path("x"), arms="solo-cheap", judge_models=["j"])
    report = build_report(_record(cfg), items, gate=gate, baseline="solo", rules=FAST)
    by_scope = {c.scope: {v.outcome for v in c.verdicts} for c in report.stats.comparisons}
    assert by_scope["all"] == {"blocked"} and by_scope["frontend"] == {"blocked"}
    assert "blocked" not in by_scope["code_review"]
    assert any("Headline verdict blocked" in n for n in report.notes)
    text = render_markdown(report)
    assert "⊘" in text and "## Judge reliability" in text
    assert "Headline verdict blocked" in render_html(report)


def test_an_allowed_gate_blocks_nothing() -> None:
    cfg = config(Path("x"), arms="solo-cheap")
    gate = JudgeGate(blocked=False, floor=0.8, accuracy={"j": 0.9})
    report = build_report(_record(cfg), two_arms(12), gate=gate, baseline="solo", rules=FAST)
    assert all(v.outcome != "blocked" for c in report.stats.comparisons for v in c.verdicts)


# ------------------------------------------------------------------------------- notes


def test_the_report_states_every_caveat_that_applies() -> None:
    cfg = config(Path("x"), arms="solo-cheap", repeats=1)
    record = _record(cfg).model_copy(update={"status": "stopped", "stop_reason": "cap reached"})
    items = [
        item("a", f"t{n}", 0.9, cost_known=False, latency_valid=False, cache_hits=2)
        for n in range(3)
    ] + [item("a", "tx", None, status="error")]
    report = build_report(record, items, rules=FAST)
    joined = " ".join(report.notes)
    for expected in (
        "Simulated run",
        "is stopped",
        "cap reached",
        "lower bound",
        "ended in an error",
        "response cache",
        "1 repeat(s)",
    ):
        assert expected in joined


def test_a_live_run_is_not_called_simulated() -> None:
    cfg = config(Path("x"), arms="solo-cheap", mock=False)
    report = build_report(_record(cfg, mock=False), two_arms(12), baseline="solo", rules=FAST)
    assert not any("Simulated" in n for n in report.notes)
    assert "(simulated)" not in render_html(report)


# ------------------------------------------------------------------------------ compare


def test_identical_runs_compare_as_not_worse_and_not_better(
    study: tuple[BenchStore, BenchRunRecord, list[BenchItem]],
) -> None:
    store, record, items = study
    meta = load_meta(store.run_dir("r1"))
    result = compare_runs((record, items, meta), (record, items, meta), rules=FAST)
    assert result.shared_tasks == 12 and len(result.changes) == 3
    for change in result.changes:
        diff = change.comparison.quality.difference
        assert diff is not None and diff.estimate == 0.0
        outcome = {v.claim: v.outcome for v in change.comparison.verdicts}
        assert outcome["not_worse"] == "yes" and outcome["better"] == "no"
    text = render_comparison(result)
    assert "Before and after: r1 → r1" in text and "solo-cheap" in text


def test_compare_pairs_each_arm_with_itself_on_the_shared_tasks() -> None:
    cfg = config(Path("x"), arms="solo-cheap")
    before = [i for i in two_arms(14, effect=0.0, seed=1) if i.arm == "fusion"]
    better = [
        i.model_copy(
            update={
                "metrics": i.metrics.model_copy(
                    update={"quality": min(1.0, (i.metrics.quality or 0) + 0.2), "cost_usd": 0.005}
                )
            }
        )
        for i in before
        if i.task_id != "t013"  # the second run lacks one task
    ]
    result = compare_runs(
        (_record(cfg, "a"), before, None), (_record(cfg, "b"), better, None), rules=FAST
    )
    assert result.shared_tasks == 13 and [c.arm for c in result.changes] == ["fusion"]
    change = result.changes[0]
    assert change.n_tasks == 13
    assert change.comparison.quality.difference is not None
    assert change.comparison.quality.difference.estimate > 0.1
    outcome = {v.claim: v.outcome for v in change.comparison.verdicts}
    assert outcome["better"] == "yes" and outcome["cheaper"] == "yes"
    assert any("no dataset snapshot" in n.lower() for n in result.notes)


def test_compare_lists_arms_only_one_run_has_and_warns_about_mixed_runs() -> None:
    cfg = config(Path("x"), arms="solo-cheap")
    a = [item("old", f"t{n}", 0.5) for n in range(12)]
    b = [item("new", f"t{n}", 0.5) for n in range(12)]
    result = compare_runs(
        (_record(cfg, "a", mock=True), a, None), (_record(cfg, "b", mock=False), b, None)
    )
    assert result.only_before == ["old"] and result.only_after == ["new"] and not result.changes
    assert any("simulated" in n for n in result.notes)
    text = render_comparison(result)
    assert "Only in a: old" in text and "Only in b: new" in text


def test_compare_with_no_shared_task_says_so() -> None:
    cfg = config(Path("x"), arms="solo-cheap")
    result = compare_runs(
        (_record(cfg, "a"), [item("x", "t1", 0.5)], None),
        (_record(cfg, "b"), [item("x", "t2", 0.5)], None),
    )
    assert result.shared_tasks == 0 and any("share no task" in n for n in result.notes)


def test_compare_notes_differing_datasets_and_repeats(
    study: tuple[BenchStore, BenchRunRecord, list[BenchItem]],
) -> None:
    store, record, items = study
    meta = load_meta(store.run_dir("r1"))
    assert isinstance(meta, RunMeta)
    other = meta.model_copy(update={"dataset_hash": "0" * 16})
    record_b = record.model_copy(update={"config": record.config.model_copy(update={"repeats": 5})})
    notes = compare_runs((record, items, meta), (record_b, items, other), rules=FAST).notes
    assert any("different datasets" in n for n in notes)
    assert any("Repeats differ" in n for n in notes)


# -------------------------------------------------------------------------------- cli


def _invoke(*args: str) -> Any:
    return runner.invoke(app, ["bench", *args])


@pytest.fixture
def run_id(fusion_home: Path) -> str:
    result = _invoke("run", "-d", "toy", "--arms", "default", "--mock", "--repeats", "2")
    assert result.exit_code == 0, result.output
    return next(p.name for p in (fusion_home / "project" / "bench-results").glob("bench-*"))


def test_report_prints_markdown_by_default(run_id: str) -> None:
    result = _invoke("report", run_id, "--min-tasks", "5", "--resamples", "200")
    assert result.exit_code == 0, result.output
    assert f"# Benchmark report: {run_id}" in result.output and "## Methodology" in result.output
    assert "solo-frontier" in result.output and "Simulated run" in result.output


def test_report_json_is_the_full_report(run_id: str) -> None:
    result = _invoke("report", run_id, "--format", "json", "--resamples", "200")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["run"]["run_id"] == run_id and data["stats"]["baseline"] == "solo-frontier"
    assert {a["arm"] for a in data["stats"]["arms"]} >= {"solo-cheap", "panel-cheap"}
    assert data["meta"]["dataset_hash"] and data["notes"]


def test_report_html_writes_one_file_into_the_run_folder(fusion_home: Path, run_id: str) -> None:
    result = _invoke("report", run_id, "--format", "html", "--resamples", "200")
    assert result.exit_code == 0, result.output
    path = fusion_home / "project" / "bench-results" / run_id / "report.html"
    assert path.is_file() and "self-contained" in result.output
    assert "https://" not in path.read_text() and "<svg" in path.read_text()
    target = fusion_home / "out" / "r.html"
    result = _invoke("report", run_id, "-f", "html", "-o", str(target), "--resamples", "200")
    assert result.exit_code == 0 and target.is_file()


def test_report_writes_markdown_to_a_file_on_request(fusion_home: Path, run_id: str) -> None:
    target = fusion_home / "docs" / "r.md"
    result = _invoke("report", run_id, "-o", str(target), "--resamples", "200")
    assert result.exit_code == 0 and target.read_text().startswith("# Benchmark report")


def test_report_baseline_and_margin_options_are_used(run_id: str) -> None:
    result = _invoke(
        "report", run_id, "-f", "json", "--baseline", "panel-cheap", "--margin", "0.1",
        "--resamples", "200",
    )  # fmt: skip
    data = json.loads(result.output)
    assert data["stats"]["baseline"] == "panel-cheap" and data["stats"]["margin"] == 0.1


def test_report_errors_are_plain(run_id: str) -> None:
    assert "'nope'" in _invoke("report", run_id, "--baseline", "nope").output
    bad = _invoke("report", run_id, "--format", "pdf")
    assert bad.exit_code == 1 and "md, html or json" in bad.output
    missing = _invoke("report", "no-such-run")
    assert missing.exit_code == 1 and "no-such-run" in missing.output


def test_report_of_a_run_with_nothing_finished_says_so(fusion_home: Path) -> None:
    store = BenchStore(fusion_home / "project" / "bench-results")
    cfg = config(Path("toy"), arms="solo-cheap")
    store.create_run("empty", cfg, 4)
    result = _invoke("report", "empty")
    assert result.exit_code == 1 and "no finished items" in result.output


def test_compare_runs_from_the_command_line(fusion_home: Path, run_id: str) -> None:
    second = _invoke("run", "-d", "toy", "--arms", "default", "--mock", "--repeats", "2")
    assert second.exit_code == 0
    ids = sorted(p.name for p in (fusion_home / "project" / "bench-results").glob("bench-*"))
    assert len(ids) == 2
    result = _invoke("compare", ids[0], ids[1], "--min-tasks", "5", "--resamples", "200")
    assert result.exit_code == 0, result.output
    assert "Before and after" in result.output and "solo-frontier" in result.output
    as_json = json.loads(_invoke("compare", ids[0], ids[1], "-f", "json").output)
    assert as_json["shared_tasks"] == 8 and len(as_json["changes"]) == 6
    assert _invoke("compare", ids[0], "nope").exit_code == 1
