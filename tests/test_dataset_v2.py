"""Dataset v2 (roadmap Task 14): its shape and promise, the frozen split, the owner's review sheet,
real-repo provenance, and the check that hidden material never reaches a prompt."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from fusion.bench.cli import bench_app
from fusion.bench.datasets.build import compile_dir, render_jsonl
from fusion.bench.datasets.coding import SOURCE_KEYS, load_task_dir
from fusion.bench.datasets.manifest import (
    REVIEW_FILE,
    SPLITS_FILE,
    check_review,
    check_splits,
    freeze_splits,
    load_review,
    load_splits,
    update_review,
)
from fusion.bench.datasets.validate import Rules, validate_dataset
from fusion.bench.spec import DatasetError, dataset_sources, load_dataset
from test_coding_dataset import write_task

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "evals" / "datasets" / "v1"
V2 = ROOT / "evals" / "datasets" / "v2"
CATEGORIES = (
    "code_review",
    "debugging",
    "architecture",
    "planning",
    "coding",
    "frontend",
    "performance",
)


def messages(report: object) -> str:
    return "\n".join(str(i) for i in report.issues)  # type: ignore[attr-defined]


# -- the shipped dataset -------------------------------------------------------------------------


def test_v2_passes_its_own_release_rules() -> None:
    report = validate_dataset(V2, Rules(release=True, run_code=False))  # structure, not execution
    assert report.ok, messages(report)
    stats = report.stats
    assert stats.tasks >= 120
    assert all(stats.by_category[c] >= 15 for c in CATEGORIES)
    assert sum(stats.real_repo.values()) >= 30
    assert stats.real_repo["dev"] >= 12 and stats.real_repo["test"] >= 12


@pytest.mark.slow
def test_v2_executable_tasks_behave_under_their_hidden_tests() -> None:
    """Runs every executable task's solution, unpatched files and flaws in the sandbox (minutes)."""
    report = validate_dataset(V2, Rules(release=True))
    assert report.ok, messages(report)


def test_v2_keeps_v1s_tasks_and_their_splits() -> None:
    old = {t.id: t for t in load_dataset(V1)}
    new = {t.id: t for t in load_dataset(V2)}
    assert set(old) <= set(new)
    assert all(new[i].split == t.split for i, t in old.items())  # nothing tuned on moved to test


def test_v2_jsonl_is_exactly_what_its_sources_compile_to() -> None:
    compiled = compile_dir(ROOT / "evals" / "datasets" / "authoring" / "v2")
    for category, tasks in compiled.tasks.items():
        path = V2 / f"{category}.jsonl"
        assert path.read_text(encoding="utf-8") == render_jsonl(tasks), path.name


def test_v2_split_is_frozen_and_every_task_is_on_the_review_sheet() -> None:
    tasks = load_dataset(V2)
    lock = load_splits(V2)
    assert lock is not None and not check_splits(lock, tasks)
    sheet = load_review(V2)
    assert sheet is not None and {e.id for e in sheet.tasks} >= {t.id for t in tasks}
    assert not [p for p in check_review(sheet, tasks, release=True) if p[0] == "error"]


def test_every_real_repo_task_says_where_it_comes_from_and_carries_its_licence() -> None:
    real = [d for d in sorted((V2 / "coding").iterdir()) if d.name.startswith("real-")]
    assert len(real) >= 30
    for task_dir in real:
        manifest = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
        assert manifest["provenance"] == "real-repo"
        source = manifest["source"]
        assert all(source.get(k) for k in SOURCE_KEYS), task_dir.name
        assert len(source["commit"]) == 40 and len(source["parent"]) == 40
        assert (task_dir / "LICENSE").is_file()
        row = load_task_dir(task_dir)
        assert "real-repo" in row["tags"] and "llm-authored" not in row["tags"]
        hidden = set(row["truth"]["hidden_files"])
        # the grade is the upstream test change, kept apart from the tests the model can see
        assert all(t.split("::")[0] in hidden for t in row["truth"]["expected_pass"])
        assert not hidden & set(row["files"])
        assert set(manifest["fail_to_pass"]) <= set(manifest["expected_pass"])


def test_hidden_material_never_reaches_a_v2_prompt() -> None:
    report = validate_dataset(V2, Rules(run_code=False))
    assert not [i for i in report.issues if "hidden material" in i.message]


# -- the frozen split ----------------------------------------------------------------------------


def _dataset(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    (root / "README.md").write_text("Licence: MIT\n")
    write_task(root, "coding-a", split="dev")
    write_task(root, "coding-b", split="test")
    return root


def test_a_frozen_split_never_changes(tmp_path: Path) -> None:
    root = _dataset(tmp_path)
    lock = freeze_splits(root, load_dataset(root))
    assert lock.splits == {"coding-a": "dev", "coding-b": "test"}
    with pytest.raises(DatasetError, match="frozen"):
        freeze_splits(root, load_dataset(root))
    manifest = root / "coding-a" / "task.yaml"
    data = yaml.safe_load(manifest.read_text())
    manifest.write_text(yaml.safe_dump({**data, "split": "test"}))
    report = validate_dataset(root, Rules(run_code=False))
    assert "a frozen split never changes" in messages(report)


def test_a_new_task_must_be_added_to_the_frozen_split(tmp_path: Path) -> None:
    root = _dataset(tmp_path)
    freeze_splits(root, load_dataset(root))
    write_task(root, "coding-c", split="test")
    assert "not in the frozen split" in messages(validate_dataset(root, Rules(run_code=False)))
    lock = freeze_splits(root, load_dataset(root), add=True)
    assert lock.splits["coding-c"] == "test"
    assert check_splits(lock, load_dataset(root)) == []


def test_metadata_files_are_never_read_as_tasks(tmp_path: Path) -> None:
    root = _dataset(tmp_path)
    update_review(root, load_dataset(root))
    (root / "_dataset.yaml").write_text("name: x\nrelease: {min_total: 1}\n")
    assert {p.name for p in dataset_sources(root)} == {"coding-a", "coding-b"}
    (root / "_dataset.yaml").write_text("release: {min_poems: 3}\n")
    assert "unknown release rule" in messages(validate_dataset(root, Rules(run_code=False)))


def test_the_manifest_states_the_release_promise(tmp_path: Path) -> None:
    root = _dataset(tmp_path)
    (root / "_dataset.yaml").write_text("release: {min_total: 5, min_real_repo: 1}\n")
    text = messages(validate_dataset(root, Rules(release=True, run_code=False)))
    assert "2 tasks; at least 5 needed" in text and "0 real-repo tasks" in text
    assert "the split is not frozen" in text


# -- the owner's review sheet --------------------------------------------------------------------


def test_the_review_sheet_lists_every_task_and_keeps_the_owners_decisions(tmp_path: Path) -> None:
    root = _dataset(tmp_path)
    sheet = update_review(root, load_dataset(root))
    assert [(e.id, e.decision) for e in sheet.tasks] == [
        ("coding-a", "pending"),
        ("coding-b", "pending"),
    ]
    text = (root / REVIEW_FILE).read_text()
    assert text.startswith("# Owner review") and "accept" in text
    data = yaml.safe_load(text)
    data["tasks"][0].update(decision="accept")
    data["tasks"][1].update(decision="fix", note="the hidden test asks for more than the prompt")
    (root / REVIEW_FILE).write_text(yaml.safe_dump(data))
    write_task(root, "coding-c")
    again = update_review(root, load_dataset(root))
    assert [(e.id, e.decision) for e in again.tasks] == [
        ("coding-a", "accept"),
        ("coding-b", "fix"),
        ("coding-c", "pending"),
    ]
    found = check_review(again, load_dataset(root), release=False)
    assert (
        "warning",
        "coding-b",
        "the owner asked for a fix: " + data["tasks"][1]["note"],
    ) in found
    assert ("warning", "dataset", "1 of 3 tasks await the owner's review") in found


def test_a_dropped_task_must_be_deleted_and_its_entry_stays_as_the_record(tmp_path: Path) -> None:
    root = _dataset(tmp_path)
    update_review(root, load_dataset(root))
    data = yaml.safe_load((root / REVIEW_FILE).read_text())
    data["tasks"][1]["decision"] = "drop"
    (root / REVIEW_FILE).write_text(yaml.safe_dump(data))
    text = messages(validate_dataset(root, Rules(release=True, run_code=False)))
    assert "error: coding-b: the owner dropped it: delete it" in text
    import shutil

    shutil.rmtree(root / "coding-b")
    sheet = update_review(root, load_dataset(root))
    assert ("coding-b", "drop") in [(e.id, e.decision) for e in sheet.tasks]
    assert not [p for p in check_review(sheet, load_dataset(root), release=True) if p[0] == "error"]


def test_the_review_and_freeze_commands(tmp_path: Path) -> None:
    root = _dataset(tmp_path)
    runner = CliRunner()
    froze = runner.invoke(bench_app, ["dataset", "freeze", str(root)])
    assert froze.exit_code == 0 and "1 dev, 1 test" in froze.output
    assert json.loads((root / SPLITS_FILE).read_text())["splits"]["coding-b"] == "test"
    assert runner.invoke(bench_app, ["dataset", "freeze", str(root)]).exit_code == 1
    reviewed = runner.invoke(bench_app, ["dataset", "review", str(root)])
    output = " ".join(reviewed.output.split())
    assert reviewed.exit_code == 0 and "2 pending, 0 accept" in output


# -- provenance and hidden material --------------------------------------------------------------


def test_a_real_repo_task_needs_its_source_and_licence(tmp_path: Path) -> None:
    root = _dataset(tmp_path)
    task = write_task(root, "real-x", provenance="real-repo")
    with pytest.raises(DatasetError, match="source.repo"):
        load_dataset(root)
    source = {
        "repo": "calc",
        "url": "https://example.com/calc",
        "commit": "a" * 40,
        "parent": "b" * 40,
        "licence": "MIT",
    }
    manifest = yaml.safe_load((task / "task.yaml").read_text())
    (task / "task.yaml").write_text(yaml.safe_dump({**manifest, "source": source}))
    row = load_task_dir(task)
    assert {"real-repo", "repo:calc"} <= set(row["tags"]) and "synthetic" not in row["tags"]
    text = messages(validate_dataset(root, Rules(run_code=False)))
    assert "real-x: a real-repo task needs the upstream LICENSE beside it" in text
    (task / "LICENSE").write_text("MIT License\n")
    assert "LICENSE" not in messages(validate_dataset(root, Rules(run_code=False)))
    stats = validate_dataset(root, Rules(run_code=False)).stats
    assert stats.real_repo == {"dev": 1}


def test_a_prompt_that_shows_a_hidden_test_line_is_an_error(tmp_path: Path) -> None:
    root = _dataset(tmp_path)
    hidden = (root / "coding-a" / "tests" / "test_hidden.py").read_text()
    leaked = max(hidden.splitlines(), key=len).strip()
    assert len(leaked) >= 32
    (root / "coding-a" / "prompt.md").write_text(f"Fix add. The test is: {leaked}\n")
    text = messages(validate_dataset(root, Rules(run_code=False)))
    assert "coding-a: the prompt shows hidden material" in text
    assert "coding-b: the prompt shows hidden material" not in text


def test_v2_task_counts_per_split_are_balanced() -> None:
    tasks = load_dataset(V2)
    by = Counter((t.category, t.split) for t in tasks)
    for category in CATEGORIES:
        assert by[(category, "dev")] >= 6 and by[(category, "test")] >= 6
