"""Datasets, tasks, arms and configuration of a study."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from _bench import point, write_dataset
from fusion.bench.arms import DEFAULT_ARMS, arm_book, parse_arms, resolve_arm
from fusion.bench.spec import (
    Arm,
    BenchConfig,
    BenchTask,
    DatasetError,
    load_dataset,
    resolve_dataset,
    select_tasks,
    task_hash,
)
from fusion.config.layers import ConfigError
from fusion.orchestration.strategy import load_strategy_book


def _task(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "x",
        "category": "debugging",
        "prompt": "Why does it hang?",
        "truth": {"points": [point("a", "deadlock")]},
    }
    return {**base, **kw}


def test_the_packaged_toy_dataset_loads_by_name() -> None:
    tasks = load_dataset("toy")
    assert len(tasks) >= 6
    assert {t.category for t in tasks} >= {"code_review", "debugging", "architecture", "planning"}
    assert all(t.parsed_truth().points for t in tasks)


def test_jsonl_yaml_and_directories_load(tmp_path: Path) -> None:
    (tmp_path / "a.jsonl").write_text(json.dumps(_task(id="a")) + "\n\n")
    (tmp_path / "b.yaml").write_text(
        yaml.safe_dump([_task(id="b", prompt="p2"), _task(id="c", prompt="p3")])
    )
    (tmp_path / "d.yml").write_text(yaml.safe_dump(_task(id="d", prompt="p4")))
    assert [t.id for t in load_dataset(tmp_path / "a.jsonl")] == ["a"]
    assert [t.id for t in load_dataset(tmp_path / "b.yaml")] == ["b", "c"]
    assert sorted(t.id for t in load_dataset(tmp_path)) == ["a", "b", "c", "d"]


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (_task(category="poetry"), "category"),
        (_task(prompt=""), "prompt"),
        (_task(extra=1), "extra"),
        (_task(truth={"points": [{"id": "a", "keywords": ["x"], "text": "unrelated"}]}), "keyword"),
    ],
)
def test_a_bad_task_is_reported_with_its_file(
    tmp_path: Path, row: dict[str, object], message: str
) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(DatasetError, match=message) as info:
        load_dataset(path)
    assert "bad.jsonl" in str(info.value)


def test_duplicate_ids_and_prompts_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "d.jsonl"
    path.write_text(json.dumps(_task()) + "\n" + json.dumps(_task(prompt="other")) + "\n")
    with pytest.raises(DatasetError, match="unique"):
        load_dataset(path)
    path.write_text(json.dumps(_task(id="a")) + "\n" + json.dumps(_task(id="b")) + "\n")
    with pytest.raises(DatasetError, match="prompts must be unique"):
        load_dataset(path)


def test_a_missing_dataset_says_where_it_looked() -> None:
    with pytest.raises(DatasetError, match="not found"):
        resolve_dataset("no-such-dataset")


def test_a_malformed_jsonl_line_names_its_line(tmp_path: Path) -> None:
    path = tmp_path / "d.jsonl"
    path.write_text(json.dumps(_task()) + "\n{not json\n")
    with pytest.raises(DatasetError, match=r"d\.jsonl:2"):
        load_dataset(path)


def test_task_hash_changes_with_anything_that_decides_the_answer() -> None:
    base = BenchTask.model_validate(_task())
    assert task_hash(base) == task_hash(BenchTask.model_validate(_task()))
    for change in ({"prompt": "Why does it crash?"}, {"context": "more"}, {"difficulty": "hard"}):
        assert task_hash(base) != task_hash(BenchTask.model_validate(_task(**change)))


def test_select_tasks_spreads_over_categories_and_is_repeatable(tmp_path: Path) -> None:
    tasks = load_dataset("toy")
    chosen = select_tasks(tasks, 4, seed=1)
    assert len(chosen) == 4
    assert len({t.category for t in chosen}) == 4  # one from each category first
    assert chosen == select_tasks(tasks, 4, seed=1)
    assert [t.id for t in chosen] == [t.id for t in tasks if t in chosen]  # dataset order
    assert select_tasks(tasks, None) == tasks
    assert select_tasks(tasks, 100) == tasks


def test_config_rejects_repeated_arm_names_and_bad_numbers(tmp_path: Path) -> None:
    dataset = write_dataset(tmp_path / "d.jsonl")
    arms = [Arm(name="a", strategy="solo-cheap"), Arm(name="a", strategy="panel-cheap")]
    with pytest.raises(ValueError, match="unique"):
        BenchConfig(dataset=dataset, arms=arms, max_usd=1)
    with pytest.raises(ValueError):
        BenchConfig(dataset=dataset, arms=arms[:1], max_usd=0)
    with pytest.raises(ValueError):
        BenchConfig(dataset=dataset, arms=arms[:1], max_usd=1, repeats=0)


# ----------------------------------------------------------------------------------------- arms


def test_parse_arms_understands_names_aliases_and_default() -> None:
    assert [a.name for a in parse_arms("default")] == list(DEFAULT_ARMS)
    arms = parse_arms("solo-cheap, cheap-panel=panel-cheap")
    assert [(a.name, a.strategy) for a in arms] == [
        ("solo-cheap", "solo-cheap"),
        ("cheap-panel", "panel-cheap"),
    ]
    with pytest.raises(ConfigError):
        parse_arms(" , ")


def test_the_default_arms_are_the_six_of_the_roadmap_and_all_exist() -> None:
    book = load_strategy_book()
    assert len(DEFAULT_ARMS) == 6
    assert all(name in book.strategies for name in DEFAULT_ARMS)


def test_an_arm_applies_overrides_over_its_strategy_and_is_named_after_itself() -> None:
    book = load_strategy_book()
    arm = Arm(
        name="refine-twice",
        strategy="panel-refine",
        overrides={"rounds": 3, "cascade": None},
    )
    strategy = resolve_arm(arm, book)
    assert strategy.name == "refine-twice" and strategy.rounds == 3
    assert strategy.members == book.get("panel-refine").members
    assert book.get("panel-refine").rounds == 2  # the shared strategy is untouched
    assert arm_book(book, [arm]).get("refine-twice").rounds == 3


def test_nested_overrides_merge_instead_of_replacing() -> None:
    book = load_strategy_book()
    arm = Arm(
        name="strict", strategy="panel-cascade", overrides={"cascade": {"agreement_threshold": 0.9}}
    )
    cascade = resolve_arm(arm, book).cascade
    assert cascade is not None and cascade.agreement_threshold == 0.9
    assert cascade.first == book.get("panel-cascade").cascade.first  # type: ignore[union-attr]


def test_an_invalid_override_names_the_arm_and_the_problem() -> None:
    book = load_strategy_book()
    with pytest.raises(ConfigError, match=r"Arm 'bad'.*rounds"):
        resolve_arm(Arm(name="bad", strategy="panel-cheap", overrides={"rounds": 99}), book)
    with pytest.raises(ConfigError, match="Unknown strategy"):
        resolve_arm(Arm(name="x", strategy="no-such"), book)
