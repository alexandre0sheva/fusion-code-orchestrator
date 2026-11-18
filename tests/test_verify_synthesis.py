"""verified-synth through the pipeline: members return a complete patch plus checks and
guardrails, the bench's sandbox cross-checks them, and the pick (or the selector, on a tie) is
the answer. Also the evidence boundary: grader material never reaches a proposer's prompt."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from _coding import ABSOLUTE, FILES, FIXED, SQUARED, coding_task, patch_for
from _scripted import FAST, SECURITY, WEAK, Scripted, context, pipeline, stages
from fusion.bench.patch import extract_patch, patch_text_key
from fusion.config.layers import ConfigError
from fusion.orchestration.claims import panel_answer_schema, parse_panel_answer
from fusion.orchestration.strategy import (
    BenchmarkOnlyError,
    Mode,
    Strategy,
    load_strategy_book,
)
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.verify.crosscheck import CheckEnv, CrossCheckReport, crosscheck
from fusion.verify.synthesis import (
    Candidate,
    EvidenceLeakError,
    assert_no_grader_material,
    grader_material,
)

GOOD = patch_for(FIXED)
WRONG = patch_for(SQUARED)


def chk(name: str, body: str, kind: str = "repro") -> dict[str, str]:
    return {"name": name, "language": "python", "kind": kind, "body": body}


def add_check(name: str, expr: str) -> dict[str, str]:
    return chk(name, f"from calc import add\nassert {expr}\n")


def reply(
    patch: str | None, checks: Sequence[dict[str, str]] = (), guardrails: Sequence[str] = ()
) -> str:
    return json.dumps(
        {
            "summary": "a fix",
            "claims": [],
            "patch": patch,
            "confidence": 0.7,
            "score": None,
            "none_found": False,
            "checks": list(checks),
            "guardrails": list(guardrails),
        }
    )


async def real_crosscheck(candidates: list[Candidate]) -> CrossCheckReport:
    return await asyncio.to_thread(crosscheck, candidates, CheckEnv(source=FILES, timeout_s=10))


# -- what a member returns ------------------------------------------------------------------------


def test_checks_and_guardrails_are_parsed_leniently() -> None:
    raw = reply(
        GOOD,
        [
            add_check("sum", "add(2, 3) == 5"),
            {"name": "no body"},
            add_check("b", "1"),
            add_check("c", "1"),
            add_check("d", "1"),
        ],
        ["signature of add unchanged", "", 7],
    )
    answer, valid = parse_panel_answer(raw, json.loads(raw))
    assert valid and answer.patch
    assert [c.name for c in answer.checks] == ["sum", "b", "c"]  # the broken one skipped, max 3
    assert answer.guardrails == ["signature of add unchanged", "7"]


def test_the_answer_schema_asks_for_checks_only_when_told() -> None:
    plain = panel_answer_schema()
    assert "checks" not in plain["properties"] and "guardrails" not in plain["properties"]
    with_checks = panel_answer_schema(checks=True)
    assert {"checks", "guardrails"} <= set(with_checks["required"])
    item = with_checks["properties"]["checks"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {"name", "language", "kind", "body"}


# -- the strategy ---------------------------------------------------------------------------------


def test_the_verified_synth_strategy_gives_approaches_and_runs_code() -> None:
    strategy = load_strategy_book().get("verified-synth")
    assert strategy.aggregator == "verified-synth"
    assert strategy.approaches == "auto" and strategy.lenses == "off"
    assert strategy.runs_code and not load_strategy_book().get("panel-nano-select").runs_code


@pytest.mark.parametrize(
    ("extra", "message"),
    [({"lenses": "auto"}, "picks ONE whole answer"), ({"rounds": 2}, "refinement")],
)
def test_verified_synth_refuses_lenses_and_refinement(extra: dict[str, Any], message: str) -> None:
    data = {
        "name": "x",
        "kind": "panel",
        "members": [{"model": "claude-haiku"}, {"model": "gpt-luna"}],
        "aggregator": "verified-synth",
        **extra,
    }
    with pytest.raises((ConfigError, ValueError), match=message):
        Strategy.model_validate(data)


async def test_verified_synth_is_refused_outside_a_benchmark(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkOnlyError, match="only in benchmark mode"):
        await pipeline(tmp_path).run(context(strategy="verified-synth", expects_patch=True))


# -- through the pipeline -------------------------------------------------------------------------


def majority_wrong() -> Scripted:
    return Scripted(
        {
            FAST: reply(GOOD, [add_check("sum", "add(2, 3) == 5")], ["add keeps two arguments"]),
            SECURITY: reply(WRONG, [add_check("four", "add(2, 2) == 4")]),
            WEAK: reply(WRONG, [add_check("zero", "add(0, 0) == 0 and add(2, 2) == 4")]),
        }
    )


async def test_the_cross_check_picks_the_right_patch_over_the_majority(tmp_path: Path) -> None:
    provider = majority_wrong()
    result = await pipeline(tmp_path, provider).run(
        context(strategy="verified-synth", expects_patch=True, crosscheck=real_crosscheck),
        mode=Mode.BENCHMARK,
    )
    assert patch_text_key(extract_patch(result.final_answer).text or "") == patch_text_key(GOOD)
    record = result.disagreement["crosscheck"]
    assert record["winner"] == FAST and record["matrix"][SECURITY][f"{FAST}/sum"] == "fail"
    assert record["guardrails"] == {FAST: ["add keeps two arguments"]}
    assert result.disagreement["verified"][FAST] == pytest.approx(1.0)
    assert "synthesis" not in stages(result)  # a clear winner needs no selector
    assert any("cross-check picked" in r for r in result.routing.reasons)
    panel = [r for r in provider.requests if r.metadata.get("role") == "panel"]
    assert panel and all("## Checks" in r.user_prompt for r in panel)
    assert all("checks" in (r.response_schema or {}).get("properties", {}) for r in panel)


async def test_a_tie_goes_to_the_selector_with_only_the_tied_candidates(tmp_path: Path) -> None:
    same = [add_check("sum", "add(2, 3) == 5")]
    provider = Scripted(
        {
            FAST: reply(GOOD, same),
            SECURITY: reply(patch_for(ABSOLUTE), same),
            WEAK: reply("--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-x\n+y\n", same),
        }
    )
    result = await pipeline(tmp_path, provider).run(
        context(strategy="verified-synth", expects_patch=True, crosscheck=real_crosscheck),
        mode=Mode.BENCHMARK,
    )
    assert result.disagreement["crosscheck"]["winner"] is None
    assert sorted(result.disagreement["crosscheck"]["tied"]) == sorted([FAST, SECURITY])
    assert result.disagreement["selection"]["candidates"] == 2
    assert "synthesis" in stages(result)
    assert extract_patch(result.final_answer).text is not None


async def test_without_the_sandbox_it_says_so_and_selects(tmp_path: Path) -> None:
    result = await pipeline(tmp_path, majority_wrong()).run(
        context(strategy="verified-synth", expects_patch=True), mode=Mode.BENCHMARK
    )
    assert any("needs the benchmark's sandbox" in w for w in result.warnings)
    assert extract_patch(result.final_answer).text is not None


# -- the evidence boundary ------------------------------------------------------------------------


def test_grader_material_is_the_hidden_tests_and_summaries_not_what_the_model_sees() -> None:
    task = coding_task()
    material = grader_material(task.truth, task.files)
    lines = [line for found in material.values() for line in found]
    assert "self.assertEqual(add(-1, 1), 0)" in lines
    assert "Make add return the product" in lines  # a flaw's summary
    assert not any("import unittest" in line for line in lines)  # the visible test has it too
    assert_no_grader_material(task.prompt + "\n" + json.dumps(task.files), material)
    with pytest.raises(EvidenceLeakError, match="hidden test"):
        assert_no_grader_material("please make   self.assertEqual(add(-1, 1), 0) pass", material)


def test_no_proposer_prompt_in_a_bench_run_contains_grader_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _bench import config, sim_env
    from fusion.bench.runner import run_bench
    from fusion.bench.virtual import run_virtual
    from fusion.providers.simulated import SimulatedProvider

    task = coding_task()
    dataset = tmp_path / "tiny.jsonl"
    dataset.write_text(json.dumps(task.model_dump(mode="json")) + "\n")
    seen: list[ModelRequest] = []
    original = SimulatedProvider.complete

    async def spy(self: SimulatedProvider, request: ModelRequest) -> ModelResponse:
        seen.append(request)
        return await original(self, request)

    monkeypatch.setattr(SimulatedProvider, "complete", spy)
    cfg = config(dataset, arms="verified-synth", repeats=1)
    run = run_virtual(run_bench(cfg, env=sim_env(cfg, tmp_path / "results"), run_id="v"))
    assert run.items and all(i.error is None for i in run.items), [i.error for i in run.items]
    material = grader_material(task.truth, task.files)
    proposers = [r for r in seen if r.metadata.get("role") in {"panel", "refine"}]
    assert proposers
    for request in proposers:
        assert_no_grader_material(request.system_text() + request.user_prompt, material)
    record = run.items[0].verification  # the matrix and the reason are in the run record
    assert record is not None and record["matrix"] and record["checks"] and record["reason"]


# -- simulated models write checks ----------------------------------------------------------------


def _sim_answer(skill: float, seed: int, *, checks: bool) -> Any:
    from fusion.bench.virtual import run_virtual
    from fusion.providers.simulated import SimModel, SimulatedProvider, SimWorld

    task = coding_task()
    provider = SimulatedProvider("sim", {"m": SimModel(skill=skill)}, SimWorld([task], seed=0))
    request = ModelRequest(
        model_id="m",
        system_prompt="s",
        user_prompt=f"## Task\n{task.prompt}\n",
        seed=seed,
        metadata={"role": "panel"},
        response_schema=panel_answer_schema(checks=checks),
    )
    response = run_virtual(provider.complete(request))
    return parse_panel_answer(response.text, response.parsed_json)[0]


def test_simulated_models_write_checks_only_when_asked() -> None:
    from fusion.verify.validate import validate_check

    assert _sim_answer(0.9, 1, checks=False).checks == []
    answer = _sim_answer(0.9, 1, checks=True)
    assert 1 <= len(answer.checks) <= 3 and answer.guardrails
    assert all(validate_check(c) == [] for c in answer.checks)


def test_a_skilled_simulated_models_checks_tell_the_fix_from_a_flaw() -> None:
    task = coding_task()
    for seed in range(3):
        answer = _sim_answer(0.97, seed, checks=True)
        candidates = [
            Candidate("fix", task.truth["solution"], list(answer.checks)),
            Candidate("flaw", task.truth["flaws"][0]["patch"], []),
        ]
        report = crosscheck(candidates, CheckEnv(source=task.files, timeout_s=10))
        assert report.kept, seed  # its checks fail on the code as it is
        assert report.winner == "fix", (seed, report.reason)


async def test_on_a_task_without_a_patch_it_asks_for_no_checks_and_selects(tmp_path: Path) -> None:
    provider = Scripted()
    result = await pipeline(tmp_path, provider).run(
        context(strategy="verified-synth", crosscheck=real_crosscheck), mode=Mode.BENCHMARK
    )
    panel = [r for r in provider.requests if r.metadata.get("role") == "panel"]
    assert panel and not any("## Checks" in r.user_prompt for r in panel)
    assert "crosscheck" not in result.disagreement
    assert "selection" in result.disagreement
