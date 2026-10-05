"""Aggregators that make no model call: digest, vote and best_of."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from _scripted import (
    AGREED,
    FAST,
    OTHER,
    SECURITY,
    UNRELATED,
    WEAK,
    Scripted,
    answer,
    context,
    pipeline,
    stages,
)
from fusion.mcp_server.server import create_mcp_server
from fusion.orchestration.aggregate import (
    aggregator_for,
    build_best_of,
    build_digest,
    build_vote,
    pick_best,
)
from fusion.orchestration.claims import (
    AgreementReport,
    ClaimCluster,
    PanelAnswer,
    agreement_score,
    cluster_claims,
    parse_panel_answer,
)
from fusion.orchestration.strategy import Strategy, load_strategy_book


def _clusters(**answers: str) -> tuple[list[ClaimCluster], AgreementReport, list[tuple[str, str]]]:
    parsed: dict[str, PanelAnswer] = {m: parse_panel_answer(t)[0] for m, t in answers.items()}
    clusters = cluster_claims(parsed)
    report = agreement_score(clusters, len(parsed))
    return clusters, report, [(m, t) for m, t in answers.items()]


# ------------------------------------------------------------------------------------- selection


def test_the_aggregator_follows_the_strategy_and_a_cascades_early_exit() -> None:
    book = load_strategy_book()
    assert aggregator_for(book.get("solo-cheap")) == "solo"
    assert aggregator_for(book.get("panel-cheap")) == "llm"
    assert aggregator_for(book.get("panel-digest")) == "digest"
    assert aggregator_for(book.get("panel-vote")) == "vote"
    cascade = book.get("panel-cascade")
    assert aggregator_for(cascade) == "llm"
    assert aggregator_for(cascade, cascade_exited_early=True) == "vote"


# ------------------------------------------------------------------------------------------ vote


def test_a_vote_keeps_only_points_a_majority_backed() -> None:
    clusters, report, _ = _clusters(a=AGREED, b=AGREED, c=UNRELATED)
    text = build_vote(clusters, report, 3).content
    assert text.startswith("## Panel vote: 3 points backed by a majority of 3 models")
    assert "exponential backoff" in text and "connection pool" not in text
    assert "1 further points were raised by one model" in text


def test_a_vote_with_nothing_agreed_says_so_instead_of_inventing_an_answer() -> None:
    clusters, report, _ = _clusters(a=AGREED, b=OTHER, c=UNRELATED)
    text = build_vote(clusters, report, 3).content
    assert "no point was backed by a majority of the 3 models" in text
    assert "panel-digest" in text


# --------------------------------------------------------------------------------------- best_of


def test_best_of_picks_the_answer_the_others_back_most() -> None:
    shared = ("finding", None, "The retry loop swallows the final exception")
    clusters, _, answers = _clusters(
        a=answer(shared, ("risk", "low", "Unrelated worry about logging volume")),
        b=answer(shared),
        c=answer(shared, ("test", None, "Add a test for the deadline")),
    )
    picked = pick_best(answers, clusters)
    assert picked.model == "b"  # all of its points are shared; a and c each carry one of their own
    assert picked.agreement == pytest.approx(1.0)


def test_best_of_breaks_ties_by_evidence_then_by_order() -> None:
    claim = ("finding", None, "The retry loop swallows the final exception")
    clusters, _, answers = _clusters(a=answer(claim), b=answer(claim))
    assert pick_best(answers, clusters).model == "a"


def test_best_of_returns_the_chosen_answer_as_written() -> None:
    clusters, _, answers = _clusters(a=AGREED, b=AGREED, c=UNRELATED)
    response, picked = build_best_of(answers, clusters)
    assert picked.model in {"a", "b"} and response.content == AGREED


def test_best_of_with_one_answer_returns_it() -> None:
    clusters, _, answers = _clusters(a=AGREED)
    assert build_best_of(answers, clusters)[1].agreement == 0.0


# ------------------------------------------------------------------------------------- the digest


def test_the_digest_tells_claude_code_it_is_the_aggregator() -> None:
    clusters, report, answers = _clusters(a=AGREED, b=OTHER)
    text = build_digest(answers, clusters, report).content
    assert text.startswith("## Panel digest: 2 answers, no synthesis model was called")
    assert "You are the aggregator" in text
    assert "### Answer from a" in text and "### Answer from b" in text


# ------------------------------------------------------------------------------------ in the run


async def test_a_vote_run_makes_no_synthesis_call(tmp_path: Path) -> None:
    provider = Scripted({FAST: AGREED, SECURITY: AGREED, WEAK: UNRELATED})
    result = await pipeline(tmp_path, provider).run(context(strategy="panel-vote"))
    assert stages(result) == ["panel"] * 3
    assert result.routing.synthesizer_model == ""
    assert result.final_answer.startswith("## Panel vote: 3 points backed by a majority of 3")
    assert result.structured_output  # the usual structured fields are still produced


async def test_a_vote_run_with_no_majority_warns(tmp_path: Path) -> None:
    provider = Scripted({FAST: AGREED, SECURITY: OTHER, WEAK: UNRELATED})
    result = await pipeline(tmp_path, provider).run(context(strategy="panel-vote"))
    assert "no point was backed by a majority" in result.final_answer
    assert any("Vote: no point" in w for w in result.warnings)


async def test_best_of_run_returns_one_answer_and_names_the_pick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-VOTE__AGGREGATOR", "best_of")
    provider = Scripted({FAST: UNRELATED, SECURITY: AGREED, WEAK: AGREED})
    result = await pipeline(tmp_path, provider).run(context(strategy="panel-vote"))
    assert stages(result) == ["panel"] * 3
    picks = [r for r in result.routing.reasons if r.startswith("best_of picked")]
    assert picks and ("mock-security" in picks[0] or "mock-weak" in picks[0])
    assert "connection pool" not in result.final_answer


async def test_digest_run_still_makes_zero_synthesis_calls(tmp_path: Path) -> None:
    result = await pipeline(tmp_path, Scripted()).run(context(strategy="panel-digest"))
    assert "synthesis" not in stages(result)
    assert "You are the aggregator" in result.final_answer


# -------------------------------------------------------------------------------- tool guidance


async def test_tool_descriptions_explain_how_to_consume_a_digest(tmp_path: Path) -> None:
    server = create_mcp_server(db_path=str(tmp_path / "runs.db"))
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        for name in ("fusion_ask", "fusion_review_diff", "fusion_debug_error"):
            schema = str(tools[name].input_schema)
            assert "panel-digest" in schema and "you are the aggregator" in schema
        assert "panel-digest" in (server.instructions or "")


def test_strategy_validation_allows_vote_and_best_of_but_not_with_a_model() -> None:
    Strategy.model_validate(
        {
            "name": "t",
            "kind": "panel",
            "members": [{"model": "a"}, {"model": "b"}],
            "aggregator": "vote",
        }
    )
    with pytest.raises(ValueError, match="best_of aggregator calls no model"):
        Strategy.model_validate(
            {
                "name": "t",
                "kind": "panel",
                "members": [{"model": "a"}, {"model": "b"}],
                "aggregator": "best_of",
                "aggregator_model": "x",
            }
        )
