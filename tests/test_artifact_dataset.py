"""The frontend and performance datasets: their shape, their loader support, and the validator that
proves each task tells a right answer from a wrong one."""

from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

import pytest
import yaml

from _artifacts import V1, dataset_of
from fusion.bench.datasets.coding import CodingTaskError, load_task_dir
from fusion.bench.datasets.validate import Rules, validate_dataset
from fusion.bench.spec import ArtifactTruth, BenchTask, CodingTruth, load_dataset

FRONTEND, PERFORMANCE = V1 / "frontend", V1 / "performance"


def messages(path: Path, **rules: object) -> list[str]:
    report = validate_dataset(path, Rules(**rules))  # type: ignore[arg-type]
    return [f"{i.level}: {i.where}: {i.message}" for i in report.issues]


def edit_task(root: Path, task_id: str, relative: str, old: str, new: str) -> None:
    path = root / task_id / relative
    text = path.read_text(encoding="utf-8")
    assert old in text, relative
    path.write_text(text.replace(old, new), encoding="utf-8")


# -- what ships ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("folder", "category"), [(FRONTEND, "frontend"), (PERFORMANCE, "performance")]
)
def test_each_new_category_has_the_promised_size_split_and_difficulty_mix(
    folder: Path, category: str
) -> None:
    tasks = load_dataset(folder)
    assert len(tasks) >= 12 and {t.category for t in tasks} == {category}
    assert Counter(t.split for t in tasks) == {"dev": len(tasks) // 2, "test": len(tasks) // 2}
    assert {t.difficulty for t in tasks} == {"easy", "medium", "hard"}
    assert all(Counter(t.difficulty for t in tasks)[d] >= 3 for d in ("easy", "medium", "hard"))
    assert len({t.id for t in tasks}) == len(tasks) == len({t.prompt for t in tasks})


@pytest.mark.parametrize("folder", [FRONTEND, PERFORMANCE])
def test_every_task_has_a_solution_two_flaws_hidden_tests_and_an_artifact_spec(
    folder: Path,
) -> None:
    for task in load_dataset(folder):
        truth = CodingTruth.model_validate(task.truth)
        spec = ArtifactTruth.model_validate(task.truth)
        assert truth.solution and len(truth.flaws) == 2 and all(f.summary for f in truth.flaws), (
            task.id
        )
        assert len(truth.expected_pass) >= 6 and truth.solution_summary, task.id
        assert spec.hard_gates and sum(c.weight for c in spec.soft_criteria) > 0, task.id
        assert "tests/test_visible.py" in task.files, task.id  # something visible to the arms
        assert "synthetic" in task.tags and "llm-authored" in task.tags


def test_performance_tasks_carry_their_workload_and_measurement_plan() -> None:
    for task in load_dataset(PERFORMANCE):
        spec = ArtifactTruth.model_validate(task.truth)
        truth = CodingTruth.model_validate(task.truth)
        assert spec.perf is not None and len(spec.perf.sizes) >= 3 and spec.perf.samples >= 5
        assert spec.perf.script in truth.hidden_files and spec.perf.script not in task.files
        assert {g.evidence for g in spec.hard_gates} == {"tests", "perf"}
        assert "benchmark times" in task.prompt  # the prompt says speed is measured


def test_frontend_tasks_get_the_shared_test_helper_and_an_offline_brief() -> None:
    helper = (
        Path(__file__).parents[1] / "src/fusion/bench/datasets/helpers/sitecheck.py"
    ).read_text()
    for task in load_dataset(FRONTEND):
        truth = CodingTruth.model_validate(task.truth)
        assert truth.hidden_files["tests/sitecheck.py"] == helper, task.id
        assert "no network (no CDN" in task.prompt and "=== path/to/file.html ===" in task.prompt
        assert task.files["index.html"].count("TODO") == 2 and "index.html" in task.prompt
        assert ArtifactTruth.model_validate(task.truth).site is not None


def test_no_task_shows_its_hidden_files_to_the_model() -> None:
    for task in [*load_dataset(FRONTEND), *load_dataset(PERFORMANCE)]:
        truth = CodingTruth.model_validate(task.truth)
        assert not set(truth.hidden_files) & set(task.files), task.id
        shown = task.prompt + task.context + "".join(task.files.values())
        for name in ("hidden_files", "expected_pass", "bench_workload", "sitecheck"):
            assert name not in shown, (task.id, name)


def test_the_dev_and_test_splits_share_no_files_and_the_validator_agrees() -> None:
    report = validate_dataset(FRONTEND, Rules(run_code=False))
    assert report.ok, [str(i) for i in report.errors]


# -- the loader ----------------------------------------------------------------------------------


def make_dir(path: Path, **manifest: object) -> Path:
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "a.py").write_text("x = 1\n")
    (path / "prompt.md").write_text("Do the thing.\n")
    (path / "task.yaml").write_text(
        yaml.safe_dump({"id": "t", "expected_pass": ["a.b"], **manifest})
    )
    return path


def test_a_task_directory_may_name_its_category_and_helpers(tmp_path: Path) -> None:
    row = load_task_dir(make_dir(tmp_path / "t", category="frontend", helpers=["sitecheck"]))
    assert row["category"] == "frontend" and "tests/sitecheck.py" in row["truth"]["hidden_files"]
    assert "no network" in row["prompt"] and "performance" not in row["prompt"]
    plain = load_task_dir(make_dir(tmp_path / "u"))
    assert plain["category"] == "coding" and "standard library only" in plain["prompt"]


def test_artifact_keys_are_copied_into_the_truth_as_written(tmp_path: Path) -> None:
    perf = {"sizes": [10, 20], "samples": 5}
    row = load_task_dir(
        make_dir(
            tmp_path / "t",
            category="performance",
            perf=perf,
            hard_gates=[{"id": "g", "evidence": "perf"}],
        )
    )
    assert row["truth"]["perf"] == perf and row["truth"]["hard_gates"][0]["id"] == "g"


@pytest.mark.parametrize(
    ("manifest", "problem"),
    [
        ({"category": "poetry"}, "category must be one of"),
        ({"helpers": ["nope"]}, "unknown helper"),
    ],
)
def test_a_task_directory_with_a_bad_category_or_helper_is_refused(
    tmp_path: Path, manifest: dict, problem: str
) -> None:
    with pytest.raises(CodingTaskError, match=problem):
        load_task_dir(make_dir(tmp_path / "t", **manifest))


@pytest.mark.parametrize(
    "truth",
    [
        {"hard_gates": [{"id": "g", "evidence": "smell"}]},
        {"hard_gates": [{"id": "g", "evidence": "tests", "max": 1}]},
        {"soft_criteria": [{"id": "c", "source": "vibes"}]},
        {"perf": {"sizes": [10], "samples": 5}},
        {"perf": {"sizes": [20, 10], "samples": 5}},
        {"perf": {"sizes": [10, 20], "samples": 2}},
    ],
)
def test_an_ill_formed_artifact_spec_makes_the_dataset_fail_to_load(truth: dict) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 — pydantic's ValidationError is a ValueError
        BenchTask.model_validate({"id": "t", "category": "frontend", "prompt": "p", "truth": truth})


# -- the validator, on copies of shipped tasks that are then broken --------------------------------


def test_a_sound_performance_task_and_a_sound_page_validate(tmp_path: Path) -> None:
    root = dataset_of(tmp_path, "perf-unique-order", "fe-faq-accordion")
    assert messages(root) == []


ANOTHER_FAST = (
    "def unique(items):\n    seen = set()\n    out = []\n    for item in items:\n"
    "        if item not in seen:\n            seen.add(item)\n            out.append(item)\n"
    "    return out\n"
)


def test_a_performance_task_whose_starting_code_is_already_fast_is_an_error(tmp_path: Path) -> None:
    root = dataset_of(tmp_path, "perf-unique-order")
    (root / "perf-unique-order/repo/unique.py").write_text(ANOTHER_FAST)
    assert any("already meets the benchmark" in m for m in messages(root))


def test_a_performance_flaw_that_is_both_correct_and_fast_is_an_error(tmp_path: Path) -> None:
    root = dataset_of(tmp_path, "perf-unique-order")
    fast = (root / "perf-unique-order/reference/solution/unique.py").read_text()
    (root / "perf-unique-order/reference/flaw-1/unique.py").write_text(fast)
    problems = messages(root)
    assert any("flaw 1 passes every gate" in m for m in problems)
    assert not any(
        "flaw 2" in m for m in problems
    )  # the wrong-but-fast one is still caught by a test


def test_a_reference_that_is_no_faster_than_the_starter_cannot_tell_fast_from_slow(
    tmp_path: Path,
) -> None:
    root = dataset_of(tmp_path, "perf-unique-order")
    slow = (root / "perf-unique-order/repo/unique.py").read_text()
    # The "solution" is the starter with a cosmetic change: as slow as the starter it replaces.
    (root / "perf-unique-order/reference/solution/unique.py").write_text(
        slow.replace("result", "found")
    )
    assert any("already meets the benchmark" in m for m in messages(root))


def test_a_page_flaw_that_is_as_good_as_the_solution_is_an_error(tmp_path: Path) -> None:
    root = dataset_of(tmp_path, "fe-faq-accordion")
    solution = root / "fe-faq-accordion/reference/solution"
    flaw = root / "fe-faq-accordion/reference/flaw-2"
    shutil.rmtree(flaw)
    shutil.copytree(solution, flaw)
    assert any(
        "flaw 2 passes every gate" in m and "so it is not wrong" in m for m in messages(root)
    )


def test_a_page_solution_that_fails_a_hidden_test_is_an_error(tmp_path: Path) -> None:
    root = dataset_of(tmp_path, "fe-faq-accordion")
    edit_task(root, "fe-faq-accordion", "reference/solution/index.html", "<h1>", "<h2>")
    edit_task(root, "fe-faq-accordion", "reference/solution/index.html", "</h1>", "</h2>")
    assert any("reference solution" in m and "gate" in m for m in messages(root))


def test_a_starter_that_already_passes_every_gate_is_an_error(tmp_path: Path) -> None:
    root = dataset_of(tmp_path, "fe-faq-accordion")
    task = root / "fe-faq-accordion"
    for name in ("index.html", "style.css"):  # the starter is already the finished page...
        text = (task / "reference/solution" / name).read_text()
        (task / "repo" / name).write_text(text)
        (task / "reference/solution" / name).write_text(text + "\n<!-- v2 -->\n")  # ...so no change
    assert any("unpatched files already pass every gate" in m for m in messages(root))


def test_a_gate_that_reads_evidence_no_evaluator_makes_is_an_error(tmp_path: Path) -> None:
    root = dataset_of(tmp_path, "fe-faq-accordion")
    path = root / "fe-faq-accordion/task.yaml"
    spec = yaml.safe_load(path.read_text())
    spec["evaluators"] = ["tests", "static"]  # no browser evaluators, but the console gate remains
    path.write_text(yaml.safe_dump(spec))
    assert any("gate 'no-console-errors' reads console evidence" in m for m in messages(root))


def test_a_performance_task_needs_a_perf_block_with_its_workload(tmp_path: Path) -> None:
    root = dataset_of(tmp_path, "perf-unique-order")
    path = root / "perf-unique-order/task.yaml"
    spec = yaml.safe_load(path.read_text())
    spec["perf"]["script"] = "tests/missing.py"
    path.write_text(yaml.safe_dump(spec))
    assert any(
        "perf.script tests/missing.py is not among the hidden files" in m for m in messages(root)
    )
    del spec["perf"]
    path.write_text(yaml.safe_dump(spec))
    assert any("needs a perf block" in m for m in messages(root))


def test_the_release_rules_ask_for_twelve_tasks_of_each_new_category(tmp_path: Path) -> None:
    root = dataset_of(tmp_path, "perf-unique-order", "fe-faq-accordion")
    problems = messages(root, release=True, run_code=False)
    assert "error: frontend: 1 tasks; at least 12 needed" in problems
    assert "error: performance: 1 tasks; at least 12 needed" in problems
