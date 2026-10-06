"""Answers that are one code patch: claims, the patch vote, the verified aggregator (benchmark
only), and the refusal to run code outside a benchmark."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from _scripted import FAST, SECURITY, WEAK, Scripted, context, pipeline, stages
from fusion.bench.patch import extract_patch
from fusion.mcp_server.server import create_mcp_server
from fusion.orchestration.aggregate import build_patch_vote, build_verified, patches_of
from fusion.orchestration.claims import (
    PanelAnswer,
    cluster_claims,
    panel_answer_schema,
    parse_panel_answer,
    patch_text_key,
    render_panel_answer,
)
from fusion.orchestration.prompts import build_synthesis_prompt
from fusion.orchestration.strategy import BenchmarkOnlyError, Mode, load_strategy_book
from fusion.routing.classifier import TaskType

HEAD = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n"
P1 = HEAD + "+    return a + b\n"
P2 = HEAD + "+    return a * b\n"
P3 = "=== calc.py ===\ndef add(a, b):\n    return abs(a) + abs(b)\n"


def reply(patch: str | None, *claims: str) -> str:
    return json.dumps(
        {
            "summary": "a fix",
            "claims": [
                {
                    "id": f"c{i}",
                    "text": c,
                    "kind": "recommendation",
                    "severity": None,
                    "file": None,
                    "line": None,
                    "evidence": None,
                }
                for i, c in enumerate(claims, 1)
            ],
            "patch": patch,
            "confidence": 0.7,
            "score": None,
        }
    )


# -- claims ----------------------------------------------------------------------------------------


def test_a_patch_is_parsed_rendered_and_found_again() -> None:
    answer, valid = parse_panel_answer(reply(P1, "Make add return the sum"))
    assert valid and answer.patch == P1
    text = render_panel_answer(answer)
    assert "```diff" in text and P1.rstrip("\n") in text
    assert extract_patch(text).text is not None
    assert patch_text_key(extract_patch(text).text or "") == patch_text_key(P1)


def test_an_answer_without_a_patch_renders_exactly_as_before() -> None:
    answer, _ = parse_panel_answer(reply(None, "No change needed"))
    assert answer.patch is None and "```" not in render_panel_answer(answer)
    assert parse_panel_answer(reply("   ", "x"))[0].patch is None  # blank is no patch
    assert PanelAnswer(summary="s").patch is None


def test_a_patch_that_contains_backticks_cannot_close_its_own_fence() -> None:
    patch = "=== notes.md ===\nrun:\n```\nmake\n```\n"
    text = render_panel_answer(PanelAnswer(summary="s", patch=patch))
    assert text.count("````") == 2
    assert extract_patch(text).text == patch.rstrip("\n")


def test_the_schema_has_a_nullable_patch_and_stays_strict() -> None:
    schema = panel_answer_schema()
    assert "patch" in schema["properties"] and "patch" in schema["required"]
    assert schema["additionalProperties"] is False
    assert "null" in json.dumps(schema["properties"]["patch"])


def test_the_synthesis_prompt_asks_for_one_patch_only_when_the_task_wants_one() -> None:
    kwargs = {
        "task_type": TaskType.DEFAULT,
        "panel_responses": [("m", "x")],
        "disagreement_analysis": {},
    }
    assert '"patch"' not in build_synthesis_prompt(**kwargs)
    asked = build_synthesis_prompt(**kwargs, patch=True)
    assert '"patch"' in asked and "never several" in asked


# -- the patch vote --------------------------------------------------------------------------------


def _answers(**patches: str | None) -> tuple[dict[str, PanelAnswer], list[tuple[str, str]]]:
    raw = {m: reply(p, f"claim from {m}") for m, p in patches.items()}
    parsed = {m: parse_panel_answer(t)[0] for m, t in raw.items()}
    readable = [(m, render_panel_answer(a)) for m, a in parsed.items()]
    return parsed, readable


def test_the_patch_most_models_gave_wins_even_when_written_with_other_whitespace() -> None:
    answers, readable = _answers(a=P2, b=P1, c=P1.replace("\n", "  \n"))
    voted = build_patch_vote(readable, answers, cluster_claims(answers))
    assert voted is not None
    response, picked, votes = voted
    assert votes == 2 and picked.model == "b"  # earliest of the two that agree
    assert response.text == dict(readable)["b"]


def test_all_different_patches_go_to_the_answer_the_others_claims_agree_with_most() -> None:
    answers = {
        "a": parse_panel_answer(reply(P1, "use the sum of the arguments"))[0],
        "b": parse_panel_answer(reply(P2, "use the product"))[0],
        "c": parse_panel_answer(reply(P3, "use the sum of the arguments"))[0],
    }
    readable = [(m, render_panel_answer(a)) for m, a in answers.items()]
    _, picked, votes = build_patch_vote(readable, answers, cluster_claims(answers)) or (
        None,
        None,
        0,
    )
    assert votes == 1 and picked is not None and picked.model in {"a", "c"}


def test_no_patch_means_no_patch_vote() -> None:
    answers, readable = _answers(a=None, b=None)
    assert patches_of(answers) == {}
    assert build_patch_vote(readable, answers, []) is None


# -- verified --------------------------------------------------------------------------------------


async def test_the_verified_aggregator_picks_the_best_scoring_patch_not_the_most_common() -> None:
    answers, readable = _answers(a=P2, b=P2, c=P1)
    seen: list[str] = []

    async def verify(patch: str) -> float | None:
        seen.append(patch)
        return 1.0 if patch == P1 else 0.0

    result = await build_verified(readable, answers, cluster_claims(answers), verify)
    assert result is not None
    response, picked, scores = result
    assert picked.model == "c" and scores == {"a": 0.0, "b": 0.0, "c": 1.0}
    assert len(seen) == 2  # identical patches are verified once
    assert response.text == dict(readable)["c"]


async def test_ties_go_to_the_patch_more_models_gave_and_unverifiable_counts_as_zero() -> None:
    answers, readable = _answers(a=P1, b=P2, c=P2)

    async def verify(patch: str) -> float | None:
        return 0.5

    _, picked, _ = await build_verified(readable, answers, cluster_claims(answers), verify) or (
        None,
        None,
        None,
    )
    assert picked is not None and picked.model == "b"

    async def unknown(patch: str) -> float | None:
        return -1.0 if patch == P2 else None  # P2 does not apply; P1 cannot be checked

    _, picked, scores = await build_verified(
        readable, answers, cluster_claims(answers), unknown
    ) or (None, None, {})
    assert picked is not None and picked.model == "a" and scores["a"] is None


async def test_verified_has_nothing_to_choose_without_patches() -> None:
    answers, readable = _answers(a=None)

    async def verify(patch: str) -> float | None:
        raise AssertionError("nothing to verify")

    assert await build_verified(readable, answers, [], verify) is None


# -- through the pipeline --------------------------------------------------------------------------


def _scripted(**patches: str | None) -> Scripted:
    models = {"a": FAST, "b": SECURITY, "c": WEAK}
    return Scripted({models[k]: reply(p, f"claim from {k}") for k, p in patches.items()})


async def test_panel_vote_returns_the_majority_patch_for_a_patch_task(tmp_path: Path) -> None:
    result = await pipeline(tmp_path, _scripted(a=P2, b=P1, c=P1)).run(
        context(strategy="panel-vote", expects_patch=True)
    )
    assert extract_patch(result.final_answer).text is not None
    assert patch_text_key(extract_patch(result.final_answer).text or "") == patch_text_key(P1)
    assert "synthesis" not in stages(result)
    assert any("patch vote" in r for r in result.routing.reasons)


async def test_a_patch_vote_with_no_patches_falls_back_to_the_best_answer(tmp_path: Path) -> None:
    result = await pipeline(tmp_path, _scripted(a=None, b=None, c=None)).run(
        context(strategy="panel-vote", expects_patch=True)
    )
    assert any("no answer contained a patch" in w for w in result.warnings)
    assert result.final_answer


async def test_a_vote_on_an_ordinary_task_is_unchanged(tmp_path: Path) -> None:
    result = await pipeline(tmp_path, _scripted(a=P2, b=P1, c=P1)).run(
        context(strategy="panel-vote")
    )
    assert result.final_answer.startswith("## Panel vote")


async def test_best_of_n_verified_runs_the_verifier_in_benchmark_mode(tmp_path: Path) -> None:
    checked: list[str] = []

    async def verify(patch: str) -> float | None:
        checked.append(patch)
        return 1.0 if patch_text_key(patch) == patch_text_key(P3) else 0.2

    result = await pipeline(tmp_path, _scripted(a=P1, b=P1, c=P3)).run(
        context(strategy="best-of-n-verified", expects_patch=True, verifier=verify),
        mode=Mode.BENCHMARK,
    )
    assert patch_text_key(extract_patch(result.final_answer).text or "") == patch_text_key(P3)
    assert len(checked) == 2 and "synthesis" not in stages(result)
    assert any("verified picked" in r for r in result.routing.reasons)


async def test_without_a_verifier_the_verified_aggregator_says_so_and_returns_the_best_answer(
    tmp_path: Path,
) -> None:
    result = await pipeline(tmp_path, _scripted(a=P1, b=P1, c=P2)).run(
        context(strategy="best-of-n-verified", expects_patch=True), mode=Mode.BENCHMARK
    )
    assert any("needs the benchmark's test runner" in w for w in result.warnings)
    assert extract_patch(result.final_answer).text is not None


async def test_the_verified_strategy_is_refused_outside_a_benchmark(tmp_path: Path) -> None:
    assert load_strategy_book().get("best-of-n-verified").aggregator == "verified"
    with pytest.raises(BenchmarkOnlyError, match="only in benchmark mode"):
        await pipeline(tmp_path).run(context(strategy="best-of-n-verified"))


async def test_the_mcp_server_refuses_it_and_runs_nothing(tmp_path: Path) -> None:
    server = create_mcp_server(db_path=str(tmp_path / "runs.db"))
    async with Client(server) as client:
        with pytest.raises(Exception, match="benchmark mode"):
            await client.call_tool(
                "fusion_ask", {"input": {"prompt": "fix it", "strategy": "best-of-n-verified"}}
            )
