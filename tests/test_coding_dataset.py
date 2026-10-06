"""Executable coding tasks as a dataset: the directory format, the validator that runs them, the
shipped ``v1/coding`` tasks, simulated models that write patches, and a study on them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from _bench import config, sim_env
from _coding import ABSOLUTE, BUGGY, FIXED, HIDDEN, SQUARED, VISIBLE
from fusion.bench.cli import bench_app
from fusion.bench.datasets.coding import PATCH_REQUEST, is_task_dir, load_task_dir
from fusion.bench.datasets.validate import Rules, validate_dataset
from fusion.bench.patch import apply_patch, extract_patch
from fusion.bench.runner import run_bench
from fusion.bench.spec import (
    BenchTask,
    DatasetError,
    dataset_sources,
    load_dataset,
    resolve_dataset,
)
from fusion.bench.virtual import run_virtual
from fusion.orchestration.claims import parse_panel_answer
from fusion.providers.base import ModelRequest
from fusion.providers.simulated import SimModel, SimulatedProvider, SimWorld
from fusion.providers.simulated_coding import BLOB_SHARE

ROOT = Path(__file__).resolve().parents[1]
CODING = ROOT / "evals" / "datasets" / "v1" / "coding"


def write_task(
    parent: Path,
    name: str = "coding-add",
    *,
    solution: str | None = FIXED,
    flaws: tuple[str, ...] = (SQUARED, ABSOLUTE),
    hidden: str = HIDDEN,
    visible: str | None = VISIBLE,
    split: str = "dev",
    expected: list[str] | None = None,
    **manifest: Any,
) -> Path:
    root = parent / name
    (root / "repo").mkdir(parents=True)
    (root / "repo" / "calc.py").write_text(BUGGY)
    if visible is not None:
        (root / "repo" / "tests").mkdir()
        (root / "repo" / "tests" / "test_visible.py").write_text(visible)
    (root / "tests").mkdir()
    (root / "tests" / "test_hidden.py").write_text(hidden)
    (root / "prompt.md").write_text(f"Fix `add` in calc.py ({name}).\n")
    if solution is not None:
        (root / "reference" / "solution").mkdir(parents=True)
        (root / "reference" / "solution" / "calc.py").write_text(solution)
    for n, source in enumerate(flaws, 1):
        (root / "reference" / f"flaw-{n}").mkdir(parents=True)
        (root / "reference" / f"flaw-{n}" / "calc.py").write_text(source)
    data = {
        "id": name,
        "difficulty": "easy",
        "split": split,
        "tags": ["python"],
        "solution_summary": "Make add return the sum",
        "flaws": [{"summary": f"wrong fix {n}"} for n in range(1, len(flaws) + 1)],
        "expected_pass": expected
        or [
            f"tests.test_hidden.HiddenTests.{t}" for t in ("test_add", "test_big", "test_negative")
        ],
        **manifest,
    }
    (root / "task.yaml").write_text(yaml.safe_dump(data))
    return root


def messages(parent: Path, **rules: Any) -> str:
    report = validate_dataset(parent, Rules(**rules))
    return "\n".join(str(i) for i in report.issues)


# -- the directory format --------------------------------------------------------------------------


def test_a_task_directory_compiles_to_a_bench_task(tmp_path: Path) -> None:
    root = write_task(tmp_path)
    assert is_task_dir(root) and not is_task_dir(root / "repo")
    task = BenchTask.model_validate(load_task_dir(root))
    assert task.category == "coding" and task.expects_patch and task.split == "dev"
    assert task.prompt.endswith(PATCH_REQUEST) and task.prompt.startswith("Fix `add`")
    assert set(task.files) == {"calc.py", "tests/test_visible.py"}  # what the model sees
    assert {"synthetic", "llm-authored", "python"} <= set(task.tags)
    truth = task.truth
    assert list(truth["hidden_files"]) == ["tests/test_hidden.py"]  # hidden: never in the files
    assert apply_patch(task.files, truth["solution"])["calc.py"] == FIXED
    assert [apply_patch(task.files, f["patch"])["calc.py"] for f in truth["flaws"]] == [
        SQUARED,
        ABSOLUTE,
    ]
    assert truth["flaws"][0]["summary"] == "wrong fix 1"
    assert "return a + b" not in json.dumps(task.files) and truth["solution"] not in task.prompt


def test_a_broken_task_directory_is_a_dataset_error(tmp_path: Path) -> None:
    root = write_task(tmp_path)
    (root / "prompt.md").unlink()
    with pytest.raises(DatasetError, match="missing prompt.md"):
        load_dataset(tmp_path)
    (root / "prompt.md").write_text("x")
    (root / "task.yaml").write_text("- a list\n")
    with pytest.raises(DatasetError, match="must hold a mapping"):
        load_dataset(tmp_path)


def test_the_files_inside_a_task_are_never_read_as_dataset_rows(tmp_path: Path) -> None:
    root = write_task(tmp_path, "coding-a")
    (root / "repo" / "settings.yaml").write_text("this: is not a task\n")
    (root / "repo" / "rows.jsonl").write_text('{"not": "a task"}\n')
    other = tmp_path / "plain.jsonl"
    other.write_text(
        json.dumps(
            {"id": "p1", "category": "code_review", "prompt": "Review x.", "truth": {"bugs": []}}
        )
        + "\n"
    )
    assert sorted(p.name for p in dataset_sources(tmp_path)) == ["coding-a", "plain.jsonl"]
    assert sorted(t.id for t in load_dataset(tmp_path)) == ["coding-a", "p1"]
    assert [t.id for t in load_dataset(root)] == ["coding-a"]  # a task directory is a dataset too


def test_splits_apply_to_task_directories(tmp_path: Path) -> None:
    write_task(tmp_path, "coding-dev", split="dev")
    write_task(tmp_path, "coding-test", split="test")
    assert [t.id for t in load_dataset(tmp_path, "dev")] == ["coding-dev"]
    assert [t.id for t in load_dataset(tmp_path, "test")] == ["coding-test"]
    assert len(load_dataset(tmp_path, "all")) == 2


def test_the_shipped_dataset_is_found_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    assert resolve_dataset("coding").resolve() == CODING
    assert resolve_dataset("v1").resolve() == CODING.parent
    assert resolve_dataset("code_review").name == "code_review.jsonl"


# -- the validator proves the tests tell right from wrong ------------------------------------------


def test_a_sound_task_validates(tmp_path: Path) -> None:
    write_task(tmp_path)
    (tmp_path / "README.md").write_text("Licence: MIT.\n")
    report = validate_dataset(tmp_path)
    assert report.ok and report.issues == [], messages(tmp_path)
    assert report.stats.by_category == {"coding": 1}


def test_a_flaw_that_passes_every_hidden_test_is_an_error(tmp_path: Path) -> None:
    write_task(tmp_path, flaws=(FIXED.replace("a + b", "b + a"),))
    assert "flaw 1 passes every hidden test" in messages(tmp_path)


def test_a_solution_that_fails_a_hidden_test_is_an_error(tmp_path: Path) -> None:
    write_task(tmp_path, solution=ABSOLUTE)
    text = messages(tmp_path)
    assert "reference solution fails hidden tests" in text and "test_negative" in text


def test_a_task_that_is_already_solved_is_an_error(tmp_path: Path) -> None:
    trivial = (
        "import unittest\n\nfrom calc import add\n\n\nclass HiddenTests(unittest.TestCase):\n"
        "    def test_zero(self):\n        self.assertEqual(add(2, 0), 2)\n"
    )
    write_task(tmp_path, hidden=trivial, expected=["tests.test_hidden.HiddenTests.test_zero"])
    assert "already pass every hidden test" in messages(tmp_path)


def test_hidden_files_must_not_collide_with_visible_ones(tmp_path: Path) -> None:
    root = write_task(tmp_path)
    (root / "repo" / "tests" / "test_hidden.py").write_text(VISIBLE)
    assert "collide with files the model is shown" in messages(tmp_path)


def test_a_task_needs_a_solution_a_summary_and_flaw_summaries(tmp_path: Path) -> None:
    write_task(tmp_path, solution=None)
    assert "no reference/solution" in messages(tmp_path)
    write_task(tmp_path, "coding-b", solution_summary="")
    assert "needs a solution_summary" in messages(tmp_path)


def test_a_missing_visible_test_and_missing_flaws_are_warnings(tmp_path: Path) -> None:
    write_task(tmp_path, visible=None, flaws=())
    report = validate_dataset(tmp_path)
    text = "\n".join(str(i) for i in report.warnings)
    assert "no visible tests" in text and "no flaws" in text
    assert not [e for e in report.errors if "visible" in e.message or "flaws" in e.message]


def test_the_release_rules_ask_for_thirty_coding_tasks(tmp_path: Path) -> None:
    write_task(tmp_path)
    assert "coding: 1 tasks; at least 30 needed" in messages(tmp_path, release=True)


def test_an_unlisted_expected_test_cannot_be_passed(tmp_path: Path) -> None:
    write_task(tmp_path, expected=["tests.test_hidden.HiddenTests.test_nonexistent"])
    text = messages(tmp_path)
    assert "reference solution fails hidden tests" in text and "test_nonexistent" in text


# -- the shipped tasks -----------------------------------------------------------------------------


def test_the_shipped_coding_dataset_has_the_promised_shape() -> None:
    # Running every task's solution, flaws and tests is part of test_v1_passes_the_release_rules.
    report = validate_dataset(CODING, Rules(run_code=False))
    assert report.ok, "\n".join(str(i) for i in report.errors)
    assert report.stats.tasks >= 30
    tasks = report.tasks
    assert {t.split for t in tasks} == {"dev", "test"}
    assert {t.difficulty for t in tasks} == {"easy", "medium", "hard"}
    assert all(t.expects_patch and t.category == "coding" for t in tasks)


def test_shipped_tasks_keep_their_secrets_out_of_what_a_model_sees() -> None:
    for task in load_dataset(CODING, "all"):
        truth = task.truth
        shown = task.prompt + "".join(task.files.values())
        assert not set(truth["hidden_files"]) & set(task.files), task.id
        for text in truth["hidden_files"].values():
            assert text not in shown, task.id
        assert truth["solution"] and truth["solution"] not in shown
        assert len(truth["flaws"]) >= 2 and len(truth["expected_pass"]) >= 5, task.id
        assert any(p.startswith("tests/") for p in task.files), f"{task.id} has no visible tests"


def test_shipped_visible_tests_catch_some_but_not_all_wrong_fixes() -> None:
    from fusion.bench.scoring.coding import visible_pass_fraction

    caught = total = 0
    for task in load_dataset(CODING, "all")[::2]:  # every other task keeps this test quick
        assert visible_pass_fraction(task, task.truth["solution"]) == 1.0, task.id
        for flaw in task.truth["flaws"]:
            result = visible_pass_fraction(task, flaw["patch"])
            total += 1
            caught += result is not None and result < 1.0
    assert (
        0.3 < caught / total < 1.0
    )  # a signal for the verified arm, never a replacement for hidden tests


# -- simulated models write patches ----------------------------------------------------------------


def _request(task: BenchTask, model: str, seed: int, role: str = "panel") -> ModelRequest:
    return ModelRequest(
        model_id=model,
        system_prompt="s",
        user_prompt=f"## Task\n{task.prompt}\n",
        seed=seed,
        metadata={"role": role},
    )


def _tiny() -> BenchTask:
    from _coding import coding_task

    return coding_task()


def test_the_fix_and_each_flaw_become_a_point_and_decoys_for_simulated_models() -> None:
    sim = _tiny().simulated_truth()
    assert [p.id for p in sim.points] == ["solution"] and sim.points[
        0
    ].text == "Make add return the sum"
    assert [d.id for d in sim.decoys] == ["flaw-1", "flaw-2"]


def test_a_simulated_model_answers_with_a_patch_that_applies_and_repeats_by_seed() -> None:
    task = _tiny()
    world = SimWorld([task], seed=0)
    spec = SimModel(skill=0.6, family="x", answer_tokens=300)
    provider = SimulatedProvider("sim", {"m": spec}, world)

    async def ask(seed: int) -> Any:
        return await provider.complete(_request(task, "m", seed))

    first = [run_virtual(ask(s)) for s in range(40)]
    assert [r.text for r in first] == [run_virtual(ask(s)).text for s in range(40)]  # deterministic
    patches = []
    for response in first:
        answer, valid = parse_panel_answer(response.text, response.parsed_json)
        assert valid and answer.patch
        found = extract_patch(answer.patch)
        assert found.text is not None, answer.patch
        patches.append(found.text)
        assert apply_patch(task.files, found.text)["calc.py"]
    forms = {"blobs" if p.startswith("===") else "diff" for p in patches}
    assert forms == {"blobs", "diff"}  # both ways of giving a patch occur
    blob_share = sum(p.startswith("===") for p in patches) / len(patches)
    assert abs(blob_share - BLOB_SHARE) < 0.25
    fixed = [p for p in patches if "return a + b" in p]
    assert 0 < len(fixed) < len(patches)  # some seeds find the fix, some fall for a flaw


def test_a_model_that_finds_nothing_still_proposes_the_common_wrong_fix() -> None:
    task = _tiny()
    world = SimWorld([task], seed=0)
    provider = SimulatedProvider("sim", {"m": SimModel(skill=0.0, family="x")}, world)
    response = run_virtual(provider.complete(_request(task, "m", 1)))
    patch = parse_panel_answer(response.text, response.parsed_json)[0].patch
    assert patch and "return a + b" not in patch


def test_other_tasks_get_no_patch_field_from_simulated_models() -> None:
    from fusion.bench.spec import BenchTask as Task

    review = Task.model_validate(
        {
            "id": "r",
            "category": "code_review",
            "prompt": "Review the change to billing.",
            "truth": {"points": [{"id": "p", "keywords": ["rounding"]}]},
        }
    )
    provider = SimulatedProvider("sim", {"m": SimModel()}, SimWorld([review]))
    response = run_virtual(provider.complete(_request(review, "m", 1)))
    assert "patch" not in json.loads(response.text)


# -- a study on coding tasks -----------------------------------------------------------------------


def _write_jsonl(path: Path, count: int) -> Path:
    rows = []
    for n in range(count):
        row = _tiny().model_dump(mode="json")
        row["id"] = f"tiny-{n}"
        row["prompt"] = f"Fix `add` in calc.py so that it returns the sum (variant {n})."
        rows.append(json.dumps(row))
    path.write_text("\n".join(rows) + "\n")
    return path


def _run(cfg: Any, tmp: Path, run_id: str = "r1") -> Any:
    return run_virtual(run_bench(cfg, env=sim_env(cfg, tmp), run_id=run_id))


def test_a_study_scores_every_arm_by_running_the_hidden_tests(tmp_path: Path) -> None:
    data = _write_jsonl(tmp_path / "tiny.jsonl", 6)
    cfg = config(
        data,
        arms="solo-frontier,solo-cheap,panel-cheap,panel-vote,panel-cascade,best-of-n-verified",
        repeats=2,
    )
    run = _run(cfg, tmp_path / "a")
    assert run.status == "completed" and run.failed_jobs == 0 and run.done_jobs == 6 * 6 * 2
    for item in run.items:
        assert item.score is not None and item.score.scorer == "tests", (item.arm, item.error)
        assert item.score.quality in (0.0, pytest.approx(2 / 3), 1.0)
        assert item.metrics.solved == (item.score.quality == 1.0)
        assert item.metrics.eval_cost_usd == 0  # tests cost nothing
        assert extract_patch(item.answer or "", None).text is not None or item.score.quality == 0.0
    by_arm = {
        a: [i.metrics.quality for i in run.items if i.arm == a] for a in {i.arm for i in run.items}
    }
    mean = {a: sum(q) / len(q) for a, q in by_arm.items()}
    assert mean["solo-frontier"] >= mean["solo-cheap"]  # skill shows through the whole harness


def test_a_study_on_patches_is_exactly_repeatable(tmp_path: Path) -> None:
    data = _write_jsonl(tmp_path / "tiny.jsonl", 3)
    cfg = config(data, arms="solo-cheap,panel-cheap,best-of-n-verified", repeats=2)

    def stable(run: Any) -> list[Any]:
        rows = [i.model_dump(mode="json", exclude={"run_id", "metrics"}) for i in run.items]
        return sorted((r["job_key"], r["answer"], r["score"]["quality"]) for r in rows)

    assert stable(_run(cfg, tmp_path / "a")) == stable(_run(cfg, tmp_path / "b", "r2"))


def test_the_verified_arm_costs_no_more_than_a_vote_and_never_calls_a_synthesizer(
    tmp_path: Path,
) -> None:
    data = _write_jsonl(tmp_path / "tiny.jsonl", 4)
    run = _run(config(data, arms="panel-vote,best-of-n-verified", repeats=1), tmp_path)
    for arm in ("panel-vote", "best-of-n-verified"):
        calls = [c.stage for i in run.items if i.arm == arm for c in i.calls]
        assert set(calls) == {"panel"}, arm  # three panelists, no synthesis, no judge


def test_the_run_command_works_on_the_shipped_coding_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION_BENCH_DIR", str(tmp_path / "results"))
    monkeypatch.chdir(ROOT)
    result = CliRunner().invoke(
        bench_app,
        [
            "run",
            "--dataset",
            "coding",
            "--arms",
            "solo-cheap,panel-cheap,best-of-n-verified",
            "--repeats",
            "1",
            "--limit",
            "5",
            "--mock",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "completed" in result.output and "0 errors" in result.output
    assert "best-of-n-verified" in result.output
