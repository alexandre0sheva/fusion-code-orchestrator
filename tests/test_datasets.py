"""Dataset tooling: the authoring compiler, the validator, the shipped v1 dataset, generation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from fusion.bench.cli import bench_app
from fusion.bench.datasets.build import (
    AUTHORING_DIR,
    AuthoringError,
    compile_dir,
    compile_item,
    render_jsonl,
    write_compiled,
)
from fusion.bench.datasets.generate import generate_candidates, run_generation
from fusion.bench.datasets.validate import Rules, validate_dataset
from fusion.bench.scoring import AnswerView, ScoreEnv, get_scorer
from fusion.bench.spec import BenchConfig, BenchTask, RubricTruth, load_dataset, task_hash
from fusion.bench.spend import SpendLedger
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.providers.base import ModelProvider, ModelRequest, ModelResponse
from fusion.routing.model_registry import ModelRegistry
from fusion.telemetry.cost import PricingRegistry

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "evals" / "datasets" / "v1"
AUTHORING = ROOT / AUTHORING_DIR

REVIEW: dict[str, Any] = {
    "id": "r1",
    "category": "code_review",
    "language": "python",
    "difficulty": "easy",
    "split": "dev",
    "title": "Add lookup",
    "description": "Adds a lookup.",
    "files": [
        {
            "path": "a.py",
            "diff": (
                '=import os\n=\n+def find(name):\n+    q = f"select {name}"«b1»\n'
                "+    return q\n-old = 1\n"
            ),
        }
    ],
    "bugs": [{"id": "b1", "category": "sql-injection", "severity": "high", "description": "d"}],
}


def review(**changes: Any) -> dict[str, Any]:
    return {**REVIEW, **changes}


# -- the compiler --------------------------------------------------------------------------------


def test_a_marker_gives_the_line_of_the_added_code_and_leaves_no_trace() -> None:
    task = compile_item(REVIEW)
    assert task.truth["bugs"] == [
        {
            "file": "a.py",
            "line": 4,
            "category": "sql-injection",
            "severity": "high",
            "description": "d",
            "aliases": [],
        }
    ]
    assert (
        task.files["a.py"]
        == 'import os\n\ndef find(name):\n    q = f"select {name}"\n    return q\n'
    )
    assert "«" not in json.dumps(task.model_dump())


def test_the_context_shows_the_diff_with_new_file_line_numbers() -> None:
    context = compile_item(REVIEW).context
    assert "=== a.py (modified) ===" in context
    assert "   4 +     q = f" in context  # the new file's line number in the gutter
    assert "     -" in context  # removed lines carry no number
    assert compile_item(review(files=[{"path": "n.py", "diff": "+x = 1\n+y = 2"}], bugs=[]))


def test_a_new_file_is_labelled_and_a_clean_change_has_empty_bugs() -> None:
    item = review(files=[{"path": "n.py", "diff": "+x = 1\n+y = 2\n"}], bugs=[])
    task = compile_item(item)
    assert "(new file)" in task.context
    assert task.truth == {"bugs": []}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"bugs": []}, "markers without a bug entry"),
        (
            {"bugs": [{"id": "zz", "category": "c", "description": "d"}]},
            "has no «zz» marker",
        ),
        ({"files": [{"path": "a.py", "diff": "=x\n?bad\n"}]}, "must start with"),
        ({"files": [{"path": "a.py", "diff": "-gone«b1»\n+new\n"}]}, "removed line"),
        ({"category": "nonsense"}, "category must be one of"),
        ({"title": None, "description": None}, "item 'r1'"),
    ],
)
def test_malformed_authoring_items_are_rejected_with_the_item_id(
    changes: dict[str, Any], message: str
) -> None:
    item = review(**changes)
    if changes.get("title", 1) is None:
        del item["title"]
    with pytest.raises(AuthoringError, match=message):
        compile_item(item)


def test_debugging_and_rubric_items_compile_to_the_truth_the_scorers_read() -> None:
    debug = compile_item(
        {
            "id": "d1",
            "category": "debugging",
            "language": "go",
            "symptom": "It crashes.",
            "files": [{"path": "m.go", "content": "package m\n"}],
            "trace": "panic: x",
            "root_cause_tags": ["nil-pointer"],
            "root_cause_aliases": {"nil-pointer": ["nil deref"]},
            "fix_keywords": ["check|guard", 404],
        }
    )
    assert debug.truth["fix_keywords"] == ["check|guard", "404"]
    assert "## Failure output" in debug.context
    assert "## Logs" not in debug.context
    rubric = compile_item(
        {
            "id": "a1",
            "category": "architecture",
            "prompt": "Pick.",
            "required": [
                "plain string",
                {"text": "keyworded", "keywords": [429, "x"], "gate": True},
            ],
        }
    )
    parsed = RubricTruth.model_validate(rubric.truth)
    assert [p.id for p in parsed.required_points] == ["r1", "r2"]
    assert parsed.required_points[1].keywords == ["429", "x"]


def test_compiling_is_deterministic_and_grouped_by_category(tmp_path: Path) -> None:
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "x.yaml").write_text(yaml.safe_dump([REVIEW, review(id="r2", title="Other")]))
    compiled = compile_dir(folder)
    assert list(compiled.tasks) == ["code_review"]
    (path,) = write_compiled(compiled, tmp_path / "out")
    assert path.read_text() == render_jsonl(compiled.tasks["code_review"])
    assert [json.loads(line)["id"] for line in path.read_text().splitlines()] == ["r1", "r2"]


# -- the validator -------------------------------------------------------------------------------


def dataset(tmp_path: Path, rows: list[dict[str, Any]], *, licence: bool = True) -> Path:
    folder = tmp_path / "ds"
    folder.mkdir()
    (folder / "t.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    if licence:
        (tmp_path / "README.md").write_text("Released under the MIT licence.\n")
    return folder


def task_row(**changes: Any) -> dict[str, Any]:
    row = compile_item(REVIEW).model_dump(mode="json", exclude_none=True)
    row.update(changes)
    return row


def messages(report: Any) -> str:
    return "\n".join(str(i) for i in report.issues)


def test_a_sound_dataset_passes_and_is_counted(tmp_path: Path) -> None:
    report = validate_dataset(dataset(tmp_path, [task_row()]))
    assert report.ok, messages(report)
    assert report.stats.by_category == {"code_review": 1}
    assert report.stats.languages == {"python": 1}


def test_every_schema_error_is_reported_with_its_row(tmp_path: Path) -> None:
    rows = [task_row(id="a"), {"id": "b", "category": "nope", "prompt": "x", "truth": {}}]
    report = validate_dataset(dataset(tmp_path, rows))
    assert not report.ok
    assert "t.jsonl:2" in messages(report)


def test_duplicate_ids_and_prompts_are_errors(tmp_path: Path) -> None:
    report = validate_dataset(dataset(tmp_path, [task_row(), task_row()]))
    text = messages(report)
    assert "id used 2 times" in text
    assert "prompts must be unique" in text


def test_bug_lines_must_be_inside_the_file_and_on_an_added_line(tmp_path: Path) -> None:
    good = task_row()
    outside = json.loads(json.dumps(good))
    outside["id"], outside["prompt"] = "o", "other prompt"
    outside["truth"]["bugs"][0]["line"] = 99
    unchanged = json.loads(json.dumps(good))
    unchanged["id"], unchanged["prompt"] = "u", "another prompt"
    unchanged["truth"]["bugs"][0]["line"] = 1  # the `import os` context line
    elsewhere = json.loads(json.dumps(good))
    elsewhere["id"], elsewhere["prompt"] = "e", "third prompt"
    elsewhere["truth"]["bugs"][0]["file"] = "missing.py"
    text = messages(validate_dataset(dataset(tmp_path, [outside, unchanged, elsewhere])))
    assert "line 99 is outside a.py" in text
    assert "a.py:1 is not an added line" in text
    assert "'missing.py' is not among the task's files" in text


def test_secret_looking_strings_are_errors(tmp_path: Path) -> None:
    row = task_row()
    row["files"] = {"a.py": 'KEY = "AKIAABCDEFGHIJKLMNOP"\n' * 3}
    row["truth"]["bugs"][0]["line"] = 1
    row["context"] = ""
    assert "secret (AWS access key)" in messages(validate_dataset(dataset(tmp_path, [row])))


def test_a_licence_note_is_required_next_to_the_dataset(tmp_path: Path) -> None:
    loose = validate_dataset(dataset(tmp_path, [task_row()], licence=False))
    assert loose.ok  # a warning outside release rules
    assert "licence note" in messages(loose)
    strict = validate_dataset(
        tmp_path / "ds", Rules(release=True, min_total=1, min_per_category=0, min_languages=0)
    )
    assert any("licence note" in str(e) for e in strict.errors)


def test_dev_and_test_may_not_share_a_prompt_or_a_file_set(tmp_path: Path) -> None:
    a = task_row(id="a", split="dev")
    b = task_row(id="b", split="test", prompt="a different prompt")  # same files as a
    c = task_row(id="c", split="test", files={"z.py": "x = 1\n"}, prompt=a["prompt"])
    c["truth"] = {"bugs": []}
    text = messages(validate_dataset(dataset(tmp_path, [a, b, c])))
    assert "shares its files with a task in the dev split" in text
    assert "shares its prompt with a task in the dev split" in text


def test_a_split_must_be_on_every_task_or_none(tmp_path: Path) -> None:
    rows = [task_row(id="a", split="dev"), task_row(id="b", prompt="p2", files={"q.py": "x\n"})]
    rows[1].pop("split", None)
    rows[1]["truth"] = {"bugs": []}
    assert "has no split, but other tasks do" in messages(validate_dataset(dataset(tmp_path, rows)))


def test_a_truth_that_does_not_separate_good_from_flawed_answers_is_an_error(
    tmp_path: Path,
) -> None:
    # Both required points share one keyword, so the flawed answer (only the first point) still
    # meets every item and scores as well as the good one.
    rubric = {
        "id": "x",
        "category": "architecture",
        "prompt": "Choose carefully please.",
        "truth": {
            "required_points": [
                {"id": "r1", "text": "use a queue", "keywords": ["queue"]},
                {"id": "r2", "text": "bound the queue", "keywords": ["queue"]},
            ]
        },
    }
    report = validate_dataset(dataset(tmp_path, [rubric]))
    assert not report.ok
    assert "a flawed answer still scores" in messages(report)


def test_a_judge_only_rubric_is_a_warning_not_an_error(tmp_path: Path) -> None:
    rubric = {
        "id": "x",
        "category": "planning",
        "prompt": "Plan the change carefully please.",
        "truth": {"required_points": ["say one thing", "say another", "and a third", "fourth"]},
    }
    report = validate_dataset(dataset(tmp_path, [rubric]))
    assert report.ok
    assert "not checked offline" in messages(report)


def test_release_rules_demand_size_coverage_and_both_splits(tmp_path: Path) -> None:
    report = validate_dataset(dataset(tmp_path, [task_row()]), Rules(release=True))
    text = messages(report)
    assert "1 tasks; at least 100 needed" in text
    assert "code_review: 1 tasks; at least 25 needed" in text
    assert "debugging: 0 tasks" in text
    assert "1 languages (python)" in text
    assert "no task has a dev/test split" not in text  # this one has a split, just too few
    assert "dev tasks; at least 8 needed" in text
    assert "0 clean" not in text and "clean; at least 20%" in text
    assert "outside 30-400" in text  # the 5-line diff is too small to be a release task


# -- the shipped dataset -------------------------------------------------------------------------


def test_v1_passes_the_release_rules() -> None:
    report = validate_dataset(V1, Rules(release=True))
    assert report.ok, messages(report)
    stats = report.stats
    assert stats.tasks == 100
    assert set(stats.by_category.values()) == {25}
    assert set(stats.languages) == {"go", "python", "typescript"}
    assert stats.clean_reviews / stats.reviews >= 0.2


def test_v1_jsonl_is_exactly_what_its_sources_compile_to() -> None:
    compiled = compile_dir(AUTHORING)
    for category, tasks in compiled.tasks.items():
        path = V1 / f"{category}.jsonl"
        assert path.read_text(encoding="utf-8") == render_jsonl(tasks), (
            f"{path.name} is out of date: run `fusion bench dataset build`"
        )


def test_v1_splits_are_disjoint_and_loaded_by_name() -> None:
    dev, test, everything = (load_dataset(V1, s) for s in ("dev", "test", "all"))
    assert (len(dev), len(test), len(everything)) == (60, 40, 100)
    assert not {t.id for t in dev} & {t.id for t in test}
    assert {t.split for t in dev} == {"dev"}
    assert (
        BenchConfig(dataset=V1, arms=[{"name": "a", "strategy": "solo-cheap"}], max_usd=1).split
        == "dev"
    )  # type: ignore[list-item]


def test_a_dataset_without_splits_is_used_whole_under_any_split() -> None:
    toy = ROOT / "src" / "fusion" / "bench" / "datasets" / "toy.jsonl"
    assert len(load_dataset(toy, "dev")) == len(load_dataset(toy, "test")) == len(load_dataset(toy))


def test_the_split_label_does_not_change_a_tasks_hash() -> None:
    task = load_dataset(V1, "all")[0]
    assert task_hash(task) == task_hash(task.model_copy(update={"split": "test"}))


def test_every_v1_task_is_scorable_and_its_truth_translates_for_simulation() -> None:
    env = ScoreEnv(
        gateway=CallGateway(
            ledger=RunLedger(lambda: 0.0),
            models={},
            providers={},
            pricing=PricingRegistry(),
            truncate_prompts=False,
        )
    )

    async def check() -> None:
        for task in load_dataset(V1, "all"):
            sim = task.simulated_truth()
            scorer = get_scorer(task.category)
            perfect = "\n".join(f"- {p.text}" for p in sim.points) or "No issues found."
            wrong = "\n".join(f"- {d.text}" for d in sim.decoys)
            got = (await scorer.score(task, AnswerView(final_answer=perfect), env)).quality
            assert got >= 0.9, (task.id, got)
            if wrong:
                bad = (await scorer.score(task, AnswerView(final_answer=wrong), env)).quality
                assert bad <= 0.5, (task.id, bad)

    asyncio.run(check())


def test_clean_reviews_have_only_a_false_alarm_for_simulated_models_to_fall_for() -> None:
    clean = next(t for t in load_dataset(V1, "all") if t.truth.get("bugs") == [])
    sim = clean.simulated_truth()
    assert sim.points == []
    assert [d.id for d in sim.decoys] == ["false-alarm"]
    assert sim.decoys[0].text.startswith(next(iter(clean.files)))


# -- generation ----------------------------------------------------------------------------------


class Drafter(ModelProvider):
    """Returns the next scripted draft (a dict, or a string for malformed JSON) per call."""

    name = "drafter"

    def __init__(self, drafts: list[Any]) -> None:
        self.drafts = list(drafts)
        self.requests: list[ModelRequest] = []

    def is_available(self) -> bool:
        return True

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        draft = self.drafts.pop(0)
        if draft is None:
            return ModelResponse(provider="d", model="m", error="down", error_type="Server")
        text = draft if isinstance(draft, str) else json.dumps(draft)
        return ModelResponse(
            provider="d",
            model=request.model_id,
            text=text,
            parsed_json=draft if isinstance(draft, dict) else None,
            input_tokens=900,
            output_tokens=3000,
            latency_ms=1.0,
            cost_estimate_usd=0.01,
        )


def gateway(drafter: Drafter) -> CallGateway:
    models = ModelRegistry.for_mode(use_mock=False).models
    providers: dict[str, ModelProvider] = {e.provider: drafter for e in models.values()}
    return CallGateway(
        ledger=RunLedger(lambda: 0.0),
        models=models,
        providers=providers,
        pricing=PricingRegistry(),
        truncate_prompts=False,
    )


def draft(**changes: Any) -> dict[str, Any]:
    base = {k: v for k, v in REVIEW.items() if k not in ("id", "category", "split", "difficulty")}
    base.update(changes)
    return base


def test_generation_keeps_drafts_that_compile_and_says_why_it_rejected_the_rest() -> None:
    drafter = Drafter(
        [
            draft(),
            "not json at all",
            draft(bugs=[]),  # a marker without a bug entry
            None,
            draft(title="Same prompt"),  # compiles, but the second one repeats it
            draft(title="Same prompt"),
        ]
    )
    existing = [compile_item(review(title="Same prompt", id="old"))]
    result = asyncio.run(
        generate_candidates(
            "code_review",
            6,
            gateway=gateway(drafter),
            alias="claude-haiku",
            existing=existing[:0],
        )
    )
    assert [i["id"] for i in result.items] == ["gen-code_review-001", "gen-code_review-005"]
    reasons = "\n".join(result.rejected)
    assert "not valid JSON" in reasons
    assert "markers without a bug entry" in reasons
    assert "the call failed" in reasons
    assert "repeats an existing prompt" in reasons  # the sixth repeats the fifth
    assert all("unreviewed" in i["tags"] for i in result.items)
    assert result.cost_usd == pytest.approx(0.05)  # five calls answered, one failed
    assert "30-120 lines" in drafter.requests[0].user_prompt


def test_generation_obeys_max_usd_and_the_spend_cap_and_records_spend(tmp_path: Path) -> None:
    from _bench import config, sim_env, write_dataset

    path = write_dataset(tmp_path / "toy.jsonl", count=2)
    ledger = SpendLedger(tmp_path / "spend.json")
    env = sim_env(config(path), tmp_path / "r", spend=ledger)
    drafter = Drafter([draft()] * 3)
    env.providers.update({name: drafter for name in env.providers})

    with pytest.raises(AuthoringError, match="over --max-usd"):
        asyncio.run(run_generation(env, "code_review", 3, alias="claude-haiku", max_usd=0.0001))
    assert ledger.total() == 0.0 and drafter.requests == []

    result = asyncio.run(run_generation(env, "code_review", 3, alias="claude-haiku", max_usd=5.0))
    assert len(result.items) == 1  # three drafts, two repeat the first
    assert ledger.total() == pytest.approx(result.cost_usd)
    assert ledger.entries()[0]["task"] == "dataset-build"

    with pytest.raises(AuthoringError, match="not in the catalog"):
        asyncio.run(run_generation(env, "code_review", 1, alias="nope", max_usd=5.0))


# -- the CLI -------------------------------------------------------------------------------------


def test_validate_exits_nonzero_on_errors_and_zero_on_a_good_dataset(tmp_path: Path) -> None:
    good = CliRunner().invoke(bench_app, ["dataset", "validate", str(V1), "--release"])
    assert good.exit_code == 0, good.output
    assert "OK" in good.output and "100 tasks" in good.output
    bad = CliRunner().invoke(
        bench_app, ["dataset", "validate", str(dataset(tmp_path, [task_row(), task_row()]))]
    )
    assert bad.exit_code == 1
    assert "id used 2 times" in bad.output


def test_stats_prints_the_counts(tmp_path: Path) -> None:
    result = CliRunner().invoke(bench_app, ["dataset", "stats", str(V1)])
    assert result.exit_code == 0, result.output
    for text in ("code_review", "debugging", "architecture", "planning", "Languages:", "24%"):
        assert text in result.output


def test_build_writes_checks_and_detects_staleness(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.yaml").write_text(yaml.safe_dump([REVIEW]))
    out = tmp_path / "out"
    args = ["dataset", "build", "--authoring", str(src), "--out", str(out)]
    assert CliRunner().invoke(bench_app, [*args, "--check"]).exit_code == 1  # nothing built yet
    built = CliRunner().invoke(bench_app, args)
    assert built.exit_code == 0 and "Wrote 1 tasks" in built.output
    assert CliRunner().invoke(bench_app, [*args, "--check"]).exit_code == 0
    (src / "x.yaml").write_text(yaml.safe_dump([review(title="Changed")]))
    assert CliRunner().invoke(bench_app, [*args, "--check"]).exit_code == 1
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "x.yaml").write_text(yaml.safe_dump([review(bugs=[])]))
    result = CliRunner().invoke(bench_app, ["dataset", "build", "--authoring", str(broken)])
    assert result.exit_code == 1
    assert "markers without a bug entry" in result.output


def test_generate_needs_its_arguments() -> None:
    result = CliRunner().invoke(bench_app, ["dataset", "build", "--generate", "3"])
    assert result.exit_code == 2
    assert "--category" in result.output


def test_the_run_command_selects_a_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUSION_BENCH_DIR", str(tmp_path / "results"))
    out = CliRunner().invoke(
        bench_app,
        ["plan", "--dataset", str(V1), "--arms", "solo-cheap", "--mock", "--split", "test"],
    )
    assert out.exit_code == 0, out.output
    assert "40 tasks x 1 arms" in out.output
    default = CliRunner().invoke(
        bench_app, ["plan", "--dataset", str(V1), "--arms", "solo-cheap", "--mock"]
    )
    assert "60 tasks x 1 arms" in default.output


def test_v1_loads_as_bench_tasks() -> None:
    assert all(isinstance(t, BenchTask) for t in load_dataset(V1, "all"))
