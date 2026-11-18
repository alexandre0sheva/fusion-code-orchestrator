"""`fusion tune`: Propose–Probe–Compose over the prompt pack, on simulated models."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fusion.bench.arms import parse_arms
from fusion.bench.runner import build_env, job_key
from fusion.bench.spec import BenchConfig, load_dataset
from fusion.bench.spend import SpendLedger
from fusion.bench.virtual import run_virtual
from fusion.cli.main import app
from fusion.config.prompt_pack import default_pack, use_prompt_pack
from fusion.orchestration.strategy import load_strategy_book
from fusion.routing.model_registry import ModelRegistry
from fusion.tune import (
    Edit,
    Probe,
    TuneConfig,
    TuneResult,
    TunerReply,
    compare,
    merge_edits,
    run_tune,
    split_holdout,
)
from fusion.tune.propose import tune_material

GOOD = "PLANTED-GOOD: check each point against the input before reporting it."
NEUTRAL = "Write in plain English."
SECRET = "The ledger rounds half-cents toward the customer on refunds"


def _dataset(path: Path, *, dev: int = 12, test: int = 4, categories: tuple[str, ...] = ()) -> Path:
    rows = []
    cats = categories or ("code_review",)
    for n in range(dev + test):
        category = cats[n % len(cats)]
        rows.append(
            {
                "id": f"t{n}",
                "category": category,
                "difficulty": "medium",
                "split": "dev" if n < dev else "test",
                "prompt": f"Look at change number {n} to the ledger module before release.",
                "context": "A Python service that keeps customer balances.",
                "files": {f"ledger/m{n}.py": "def refund(c):\n    return c.total * 1.0\n"},
                "truth": {
                    "points": [
                        {"id": f"p{n}{i}", "keywords": [f"kw-{n}-{i}"], "text": f"Point kw-{n}-{i}"}
                        for i in range(8)
                    ]
                    + [{"id": f"s{n}", "keywords": ["half-cents"], "text": SECRET}],
                    "decoys": [{"id": f"d{n}", "keywords": [f"wrong-{n}"]}],
                },
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


class ScriptedTuner:
    """Proposals by axis, in order; composers return ``compose`` (or nothing)."""

    model = "scripted"

    def __init__(
        self,
        proposals: dict[str, list[dict[str, Any]]],
        compose: list[dict[str, Any]] | None = None,
        cost: float = 0.0,
    ) -> None:
        self.proposals = proposals
        self.compose = compose or []
        self.cost = cost
        self.prompts: list[str] = []

    def estimate(self, system: str, prompt: str) -> float:
        return self.cost

    async def ask(self, *, role: str, system: str, prompt: str, meta: dict[str, Any]) -> TunerReply:
        self.prompts.append(prompt)
        if role == "tune_proposer":
            items = self.proposals.get(meta["axis"], [])
            index = meta["index"]
            if index >= len(items):
                return TunerReply(text="{}", cost_usd=self.cost)
            return TunerReply(text=json.dumps(items[index]), cost_usd=self.cost)
        return TunerReply(text=json.dumps({"edits": self.compose}), cost_usd=self.cost)


def _cfg(dataset: Path, **kw: Any) -> TuneConfig:
    values: dict[str, Any] = {
        "dataset": dataset,
        "arm": "solo-haiku",
        "k": 1,
        "max_usd": 50.0,
        "mock": True,
        "limit": None,
        "min_gain": 0.02,
    }
    values.update(kw)
    return TuneConfig(**values)


def _run(
    cfg: TuneConfig,
    tmp: Path,
    tuner: ScriptedTuner,
    effects: dict[str, dict[str, float]] | None = None,
    spend: SpendLedger | None = None,
) -> TuneResult:
    tasks = load_dataset(cfg.dataset, "dev")
    bench_cfg = BenchConfig(
        dataset=cfg.dataset, arms=parse_arms(cfg.arm), max_usd=cfg.max_usd, mock=True
    )
    env = build_env(
        bench_cfg, tasks, results_dir=tmp / "results", prompt_effects=effects or {}
    )
    if spend is not None:
        env = dataclasses.replace(env, spend=spend)
    return run_virtual(run_tune(cfg, env=env, tuner=tuner, out_dir=tmp / "out"))


def _rows(result: TuneResult, stage: str) -> list[Any]:
    return [r for r in result.evidence if r.stage == stage]


def test_a_planted_good_edit_is_adopted(tmp_path: Path) -> None:
    data = _dataset(tmp_path / "d.jsonl")
    tuner = ScriptedTuner(
        {
            "inv": [{"target": "rules", "op": "append", "text": GOOD, "rationale": "ground"}],
            "dom": [
                {"target": "role.coding_reviewer", "op": "append", "text": NEUTRAL},
            ],
        }
    )
    result = _run(_cfg(data), tmp_path, tuner, effects={GOOD: {"*": 0.3}})
    proposed = {r.targets[0]: r for r in _rows(result, "propose")}
    assert proposed["rules"].verdict == "helped"
    assert proposed["role.coding_reviewer"].verdict == "no_effect"
    assert result.status == "completed"
    assert result.adopted is not None
    assert GOOD in result.adopted.texts["rules"]
    assert NEUTRAL not in result.adopted.texts["role.coding_reviewer"]
    confirm = _rows(result, "confirm")
    assert len(confirm) == 1 and confirm[0].verdict == "confirmed"
    assert confirm[0].effect is not None and confirm[0].effect.delta > 0
    out = Path(result.out_dir)
    diff = (out / "pack.diff").read_text()
    assert "+++ b/rules.md" in diff and GOOD in diff
    table = (out / "evidence.md").read_text()
    assert "helped" in table and "no_effect" in table
    assert (out / "pack" / "rules.md").read_text().strip().endswith(GOOD)
    assert json.loads((out / "tune.json").read_text())["adopted_digest"] == result.adopted.digest


def test_a_neutral_edit_is_rejected_as_within_noise(tmp_path: Path) -> None:
    data = _dataset(tmp_path / "d.jsonl")
    tuner = ScriptedTuner(
        {"inv": [{"target": "claims", "op": "append", "text": NEUTRAL}]}
    )
    result = _run(_cfg(data, axes=["inv"]), tmp_path, tuner, effects={GOOD: {"*": 0.3}})
    (row,) = _rows(result, "propose")
    assert row.verdict == "no_effect"
    assert row.effect is not None
    assert abs(row.effect.delta) <= row.effect.band
    assert result.adopted is None
    assert _rows(result, "confirm") == []
    assert (Path(result.out_dir) / "pack.diff").read_text() == ""


def test_an_edit_that_helps_one_task_type_and_hurts_another_is_confounded(
    tmp_path: Path,
) -> None:
    data = _dataset(tmp_path / "d.jsonl", dev=16, categories=("code_review", "debugging"))
    tuner = ScriptedTuner({"inv": [{"target": "rules", "op": "append", "text": GOOD}]})
    effects = {GOOD: {"code_review": 0.35, "debugging": -0.35}}
    result = _run(_cfg(data, axes=["inv"]), tmp_path, tuner, effects=effects)
    (row,) = _rows(result, "propose")
    assert row.verdict == "confounded"
    assert result.adopted is None


def test_the_budget_cap_stops_the_run(tmp_path: Path) -> None:
    data = _dataset(tmp_path / "d.jsonl")
    tuner = ScriptedTuner({"inv": [{"target": "rules", "op": "append", "text": GOOD}]})
    result = _run(_cfg(data, max_usd=0.0001), tmp_path, tuner, effects={GOOD: {"*": 0.3}})
    assert result.status == "stopped"
    assert "max_usd" in (result.stop_reason or "")
    assert result.adopted is None
    assert _rows(result, "propose") == []
    assert tuner.prompts == []


def test_the_live_spend_ledger_stops_the_run(tmp_path: Path) -> None:
    data = _dataset(tmp_path / "d.jsonl")
    ledger = SpendLedger(tmp_path / "spend.json", cap_usd=0.00001)
    tuner = ScriptedTuner({"inv": [{"target": "rules", "op": "append", "text": GOOD}]})
    result = _run(_cfg(data, mock=False), tmp_path, tuner, spend=ledger)
    assert result.status == "stopped"
    assert "live-spend cap" in (result.stop_reason or "")
    assert ledger.total() <= 0.00001 + 1e-9


def test_the_test_split_is_never_used_during_tuning(tmp_path: Path) -> None:
    data = _dataset(tmp_path / "d.jsonl", dev=12, test=6)
    tuner = ScriptedTuner({"inv": [{"target": "rules", "op": "append", "text": GOOD}]})
    result = _run(_cfg(data, axes=["inv"]), tmp_path, tuner, effects={GOOD: {"*": 0.3}})
    dev_ids = {t.id for t in load_dataset(data, "dev")}
    test_tasks = [t for t in load_dataset(data, "all") if t.split == "test"]
    assert set(result.tune_task_ids) | set(result.holdout_task_ids) <= dev_ids
    assert not set(result.tune_task_ids) & set(result.holdout_task_ids)
    seen: set[str] = set()
    for item_file in (tmp_path / "results").glob("*/results.jsonl"):
        for line in item_file.read_text().splitlines():
            seen.add(json.loads(line)["task_id"])
    assert seen and seen <= dev_ids
    for prompt in tuner.prompts:
        assert not any(t.prompt in prompt for t in test_tasks)


def test_invalid_proposals_are_rejected_before_any_probe(tmp_path: Path) -> None:
    data = _dataset(tmp_path / "d.jsonl")
    tuner = ScriptedTuner(
        {
            "inv": [
                {"target": "rules", "op": "replace", "text": "Answer in JSON."},
                {"target": "claims", "op": "append", "text": "x" * 5000},
                {"target": "claims", "op": "append", "text": f"Remember: {SECRET}."},
                {"target": "nowhere", "op": "append", "text": "x"},
            ]
        }
    )
    result = _run(_cfg(data, axes=["inv"], k=4), tmp_path, tuner)
    rows = _rows(result, "propose")
    assert [r.verdict for r in rows] == ["invalid"] * 4
    assert all(r.effect is None for r in rows)
    errors = [" ".join(r.errors) for r in rows]
    assert "{{untrusted_rules}}" in errors[0]
    assert "size" in errors[1]
    assert "grader material" in errors[2]
    assert "nowhere" in errors[3]
    assert result.probes == 1  # the base only


def test_deep_mode_carries_the_best_pack_through_the_rounds(tmp_path: Path) -> None:
    data = _dataset(tmp_path / "d.jsonl")
    second = "PLANTED-SECOND: name the test that would catch each problem."
    tuner = ScriptedTuner(
        {
            "inv": [
                {"target": "rules", "op": "append", "text": GOOD},
                {"target": "claims", "op": "append", "text": NEUTRAL},
            ],
            "dom": [{"target": "role.coding_reviewer", "op": "append", "text": second}],
        }
    )
    effects = {GOOD: {"*": 0.2}, second: {"*": 0.2}}
    result = _run(_cfg(data, mode="deep", k=3), tmp_path, tuner, effects=effects)
    rows = _rows(result, "deep")
    assert [r.verdict for r in rows] == ["helped", "helped", "no_effect"]
    assert result.adopted is not None
    assert GOOD in result.adopted.texts["rules"]
    assert second in result.adopted.texts["role.coding_reviewer"]
    assert NEUTRAL not in result.adopted.texts["claims"]


def test_the_merge_composer_combines_helpful_edits() -> None:
    base = default_pack()
    edits = [
        (Edit(target="rules", op="append", text="A"), 0.1),
        (Edit(target="rules", op="append", text="B"), 0.05),
        (Edit(target="claims", op="replace", text="C1"), 0.02),
        (Edit(target="claims", op="replace", text="C2"), 0.08),
    ]
    merged = merge_edits(edits)
    pack = base.with_edits({})
    for edit in merged:
        pack = edit.apply(pack)
    assert pack.text("rules").endswith("A\nB")
    assert pack.text("claims") == "C2"


def test_holdout_is_stratified_and_disjoint() -> None:
    tasks = load_dataset("toy")
    tune, holdout = split_holdout(tasks, 0.25, seed=3)
    assert {t.category for t in holdout} == {t.category for t in tasks}
    assert not {t.id for t in tune} & {t.id for t in holdout}
    assert len(tune) + len(holdout) == len(tasks)
    assert split_holdout(tasks, 0.25, seed=3) == (tune, holdout)


def test_tune_material_covers_truth_text_but_not_what_the_model_is_shown(tmp_path: Path) -> None:
    data = _dataset(tmp_path / "d.jsonl")
    material = tune_material(load_dataset(data, "all"))
    lines = {line for found in material.values() for line in found}
    assert SECRET in lines
    assert "A Python service that keeps customer balances." not in lines


def test_a_job_key_changes_only_under_a_non_default_pack(tmp_path: Path) -> None:
    task = load_dataset("toy")[0]
    strategy = load_strategy_book().get("solo-haiku")
    registry = ModelRegistry.for_mode(use_mock=False)
    before = job_key(task, strategy, registry, 1, 0, False)
    with use_prompt_pack(default_pack().with_edits({})):
        assert job_key(task, strategy, registry, 1, 0, False) == before
    with use_prompt_pack(default_pack().with_edits({"claims": "Be brief."})):
        assert job_key(task, strategy, registry, 1, 0, False) != before


def test_tune_refuses_a_live_run_without_a_cap() -> None:
    with pytest.raises(ValueError, match="max_usd"):
        TuneConfig(dataset=Path("toy"), mock=False)


def test_fusion_tune_mock_writes_a_pack_diff_and_an_evidence_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION_BENCH_DIR", str(tmp_path))
    result = CliRunner().invoke(
        app, ["tune", "--mock", "--dataset", "toy", "--arm", "solo-haiku", "--k", "2"]
    )
    assert result.exit_code == 0, result.output
    (out,) = (tmp_path / "tune").iterdir()
    assert (out / "pack.diff").exists()
    table = (out / "evidence.md").read_text()
    assert "| p1 |" in table
    assert "Evidence" in result.output


def _probe(label: str, quality: dict[str, float], categories: dict[str, str]) -> Probe:
    return Probe(
        label=label,
        run_id=label,
        pack_digest=label,
        status="completed",
        quality=quality,
        solved={t: 0.0 for t in quality},
        categories=categories,
    )


def test_a_task_type_with_too_few_tasks_cannot_make_an_edit_confounded() -> None:
    # One architecture task up, one review task down, the rest flat: single-task noise.
    cats = {"a1": "architecture", "r1": "code_review", **{f"x{i}": "coding" for i in range(6)}}
    base = _probe("base", dict.fromkeys(cats, 0.5), cats)
    cand = _probe("cand", {**dict.fromkeys(cats, 0.5), "a1": 0.8, "r1": 0.2}, cats)
    effect = compare(base, cand, min_gain=0.02)
    assert effect.verdict == "no_effect"
    assert "architecture" in effect.reason and "too few tasks" in effect.reason


def test_task_types_with_enough_tasks_still_make_an_edit_confounded() -> None:
    cats = {f"a{i}": "architecture" for i in range(3)}
    cats.update({f"r{i}": "code_review" for i in range(3)})
    base = _probe("base", dict.fromkeys(cats, 0.5), cats)
    up = {f"a{i}": 0.8 for i in range(3)}
    down = {f"r{i}": 0.2 for i in range(3)}
    effect = compare(base, _probe("cand", {**up, **down}, cats), min_gain=0.02)
    assert effect.verdict == "confounded"
    cand = _probe("cand", {**up, **down}, cats)
    single = compare(base, cand, min_gain=0.02, min_category_tasks=4)
    assert single.verdict != "confounded"
