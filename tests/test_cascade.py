"""Cascade: the cheapest models answer first, the rest only when they disagree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from _scripted import (
    AGREED,
    FAST,
    HIGH_RISK_PROMPT,
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
from fusion.config.catalog import load_catalog
from fusion.orchestration.cascade import CascadeOutcome, cheapest_first, decide, merge_fanouts
from fusion.orchestration.claims import AgreementReport
from fusion.orchestration.fanout import FanoutResult, PanelCallResult
from fusion.orchestration.strategy import CascadeSpec, PanelMember, load_strategy_book
from fusion.providers.base import ModelResponse
from fusion.telemetry.cost import PricingRegistry

AGREEING = {FAST: AGREED, SECURITY: AGREED, WEAK: OTHER}


# ----------------------------------------------------------------------------------- the decision


def _report(**kw: Any) -> AgreementReport:
    fields: dict[str, Any] = {
        "n_models": 2,
        "n_requested": 2,
        "score": 0.9,
        "low_information": False,
    }
    return AgreementReport(**{**fields, **kw})


SPEC = CascadeSpec(agreement_threshold=0.7)


def test_the_first_wave_is_enough_when_it_agrees_on_a_safe_task() -> None:
    stop, reason = decide(_report(score=0.7), risk="low", spec=SPEC)
    assert stop and "agree" in reason


@pytest.mark.parametrize(
    ("report", "risk", "why"),
    [
        (_report(score=0.69), "low", "below"),
        (_report(n_models=1, score=0.0, low_information=True), "low", "only 1 of the first 2"),
        (_report(contradicted=["C1"]), "low", "disputed"),
        (_report(), "high", "high risk"),
    ],
)
def test_the_cascade_escalates_unless_everything_is_in_order(
    report: AgreementReport, risk: str, why: str
) -> None:
    stop, reason = decide(report, risk=risk, spec=SPEC)
    assert not stop and why in reason


def test_high_risk_can_be_allowed_to_exit_early() -> None:
    spec = CascadeSpec(escalate_on_high_risk=False)
    assert decide(_report(), risk="high", spec=spec)[0]


# ----------------------------------------------------------------------------- cheapest members


def test_members_run_cheapest_first_by_catalog_price_and_ties_keep_their_order() -> None:
    catalog = load_catalog()
    pricing = PricingRegistry(catalog)
    members = [PanelMember(model=a) for a in ("claude-haiku", "gpt-luna", "gemini-flash")]
    ordered = cheapest_first(members, catalog.models, pricing)
    assert [m.model for m in ordered] == ["gpt-luna", "gemini-flash", "claude-haiku"]
    free = [PanelMember(model=a) for a in (WEAK, FAST, SECURITY)]
    tied = cheapest_first(free, catalog.models, pricing)
    assert [m.model for m in tied] == [WEAK, FAST, SECURITY]


def test_a_model_without_a_price_sorts_last() -> None:
    catalog = load_catalog()
    bare = catalog.models["claude-haiku"].model_copy(update={"prices": []})
    models = {**catalog.models, "claude-haiku": bare}
    members = [PanelMember(model=a) for a in ("claude-haiku", "claude-sonnet", "gpt-luna")]
    pricing = PricingRegistry(catalog.model_copy(update={"models": models}))
    ordered = cheapest_first(members, models, pricing)
    assert [m.model for m in ordered] == ["gpt-luna", "claude-sonnet", "claude-haiku"]


# ------------------------------------------------------------------------------- merging waves


def _call(name: str, ok: bool = True, ms: int = 10) -> PanelCallResult:
    response = ModelResponse(provider="mock", model=name, text="x") if ok else None
    return PanelCallResult(
        model_name=name,
        provider="mock",
        provider_model_id=name,
        status="success" if ok else "failed",
        response=response,
        error=None if ok else "down",
        latency_ms=ms,
    )


def test_merged_waves_add_their_wall_time_and_drop_the_first_waves_quorum_warning() -> None:
    first = FanoutResult(
        calls=[_call("a"), _call("b", ok=False)],
        panel_wall_latency_ms=100,
        min_successful_responses=2,
        quorum_met=False,
        warnings=["Panel model b failed: down", "Panel quorum not met: 1/2 successful responses."],
    )
    second = FanoutResult(calls=[_call("c", ms=40)], panel_wall_latency_ms=40)
    merged = merge_fanouts(first, second, order=["a", "b", "c"], min_successful=2)
    assert [c.model_name for c in merged.calls] == ["a", "b", "c"]
    assert merged.panel_wall_latency_ms == 140 and merged.max_model_latency_ms == 40
    assert merged.quorum_met and merged.min_successful_responses == 2
    assert merged.warnings == ["Panel model b failed: down"]


def test_merged_waves_report_a_quorum_that_is_still_missed() -> None:
    first = FanoutResult(calls=[_call("a", ok=False), _call("b", ok=False)])
    second = FanoutResult(calls=[_call("c")])
    merged = merge_fanouts(first, second, order=["a", "b", "c"], min_successful=2)
    assert not merged.quorum_met
    assert any("Panel quorum not met: 1/2" in w for w in merged.warnings)


# --------------------------------------------------------------------------- the pipeline runs


async def test_agreeing_cheap_models_end_the_run_with_no_synthesis_and_no_third_model(
    tmp_path: Path,
) -> None:
    provider = Scripted(AGREEING)
    result = await pipeline(tmp_path, provider).run(context(strategy="panel-cascade"))
    assert provider.models_called() == [FAST, SECURITY]  # the third member was never asked
    assert stages(result) == ["panel", "panel"]
    assert isinstance(result.cascade, CascadeOutcome)
    assert result.cascade.exited_early and result.cascade.escalated_to == []
    assert result.cascade.first_wave == [FAST, SECURITY]
    assert result.cascade.agreement == pytest.approx(1.0)
    assert result.final_answer.startswith("## Panel vote: 3 points backed by a majority of 2")
    assert "exponential backoff" in result.final_answer
    assert result.routing.synthesizer_model == ""
    assert any(r.startswith("Cascade: the first 2 models agree") for r in result.routing.reasons)
    assert result.trace.panel_models == [FAST, SECURITY]
    assert result.agreement is not None and result.agreement.coverage == 1.0
    assert result.fanout is not None and result.fanout.quorum_met


async def test_disagreeing_cheap_models_escalate_to_the_whole_panel_and_a_synthesis(
    tmp_path: Path,
) -> None:
    provider = Scripted({FAST: AGREED, SECURITY: OTHER, WEAK: UNRELATED})
    result = await pipeline(tmp_path, provider).run(context(strategy="panel-cascade"))
    assert provider.models_called("panel") == [FAST, SECURITY, WEAK]
    assert stages(result).count("synthesis") == 1
    assert result.cascade is not None and not result.cascade.exited_early
    assert result.cascade.escalated_to == [WEAK] and "below" in result.cascade.reason
    assert result.routing.synthesizer_model == "mock-judge"
    assert result.agreement is not None and result.agreement.n_models == 3
    assert [p.model_name for p in result.panel_results] == [FAST, SECURITY, WEAK]


async def test_the_second_wave_starts_after_the_first_has_finished(tmp_path: Path) -> None:
    provider = Scripted({FAST: AGREED, SECURITY: OTHER, WEAK: UNRELATED}, delay={FAST: 0.08})
    result = await pipeline(tmp_path, provider).run(context(strategy="panel-cascade"))
    spans = {s["model"]: s for s in result.ledger.timeline() if s["stage"] == "panel"}
    assert float(str(spans[WEAK]["start_ms"])) >= float(str(spans[FAST]["end_ms"])) - 5
    assert result.fanout is not None and result.fanout.panel_wall_latency_ms >= 80


async def test_a_high_risk_task_escalates_even_when_the_first_wave_agrees(tmp_path: Path) -> None:
    provider = Scripted(AGREEING)
    result = await pipeline(tmp_path, provider).run(
        context(HIGH_RISK_PROMPT, strategy="panel-cascade")
    )
    assert result.routing.risk == "high"
    assert result.cascade is not None and not result.cascade.exited_early
    assert "high risk" in result.cascade.reason
    assert provider.models_called("panel") == [FAST, SECURITY, WEAK]


async def test_a_disputed_severity_escalates(tmp_path: Path) -> None:
    claim = ("finding", "low", "The retry loop swallows the final exception")
    loud = ("finding", "critical", "The retry loop swallows the final exception")
    provider = Scripted({FAST: answer(claim), SECURITY: answer(loud), WEAK: answer(claim)})
    result = await pipeline(tmp_path, provider).run(context(strategy="panel-cascade"))
    assert result.cascade is not None and "disputed" in result.cascade.reason


async def test_a_failed_first_wave_model_means_escalation_not_an_early_answer(
    tmp_path: Path,
) -> None:
    provider = Scripted(AGREEING, fail={SECURITY})
    result = await pipeline(tmp_path, provider).run(context(strategy="panel-cascade"))
    assert result.cascade is not None and not result.cascade.exited_early
    assert "only 1 of the first 2" in result.cascade.reason
    assert result.fanout is not None and result.fanout.quorum_met  # FAST + WEAK answered
    assert not any("quorum not met" in w.lower() for w in result.warnings)
    assert any(f"Panel model {SECURITY} failed" in w for w in result.warnings)


async def test_a_cascade_whose_first_wave_cannot_answer_halts_like_any_panel(
    tmp_path: Path,
) -> None:
    provider = Scripted(AGREEING, fail={FAST, SECURITY, WEAK})
    result = await pipeline(tmp_path, provider).run(context(strategy="panel-cascade"))
    assert "quorum was not met" in result.final_answer


async def test_rounds_apply_only_when_the_cascade_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CASCADE__ROUNDS", "2")
    early = await pipeline(tmp_path, Scripted(AGREEING)).run(context(strategy="panel-cascade"))
    assert "refine" not in stages(early)
    disagreeing = Scripted({FAST: AGREED, SECURITY: OTHER, WEAK: UNRELATED})
    late = await pipeline(tmp_path, disagreeing).run(context(strategy="panel-cascade"))
    assert stages(late).count("refine") == 3


async def test_best_of_can_be_the_early_aggregator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CASCADE__CASCADE__EARLY_AGGREGATOR", "best_of")
    result = await pipeline(tmp_path, Scripted(AGREEING)).run(context(strategy="panel-cascade"))
    assert result.cascade is not None and result.cascade.exited_early
    assert "exponential backoff" in result.final_answer
    assert any(r.startswith("best_of picked") for r in result.routing.reasons)
    assert stages(result) == ["panel", "panel"]


async def test_the_threshold_is_configurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partly = {FAST: AGREED, SECURITY: OTHER, WEAK: UNRELATED}
    base = await pipeline(tmp_path, Scripted(partly)).run(context(strategy="panel-cascade"))
    assert base.cascade is not None and not base.cascade.exited_early
    monkeypatch.setenv("FUSION__STRATEGIES__PANEL-CASCADE__CASCADE__AGREEMENT_THRESHOLD", "0")
    lenient = await pipeline(tmp_path, Scripted(partly)).run(context(strategy="panel-cascade"))
    assert lenient.cascade is not None and lenient.cascade.exited_early


async def test_a_cascade_with_no_one_left_to_escalate_to_runs_as_a_panel(tmp_path: Path) -> None:
    provider = Scripted(AGREEING)
    result = await pipeline(tmp_path, provider).run(
        context(strategy="panel-cascade", max_models=2)
    )
    assert result.cascade is None
    assert stages(result) == ["panel", "panel", "synthesis"]
    assert any("ran them as one panel" in w for w in result.warnings)


async def test_a_custom_cascade_in_user_config(fusion_home: Path, tmp_path: Path) -> None:
    (fusion_home / "config" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "strategies": {
                    "mine": {
                        "kind": "cascade",
                        "members": [
                            {"model": a} for a in ("claude-haiku", "gpt-luna", "gemini-flash")
                        ],
                        "cascade": {"first": 2, "agreement_threshold": 0.5},
                        "aggregator": "digest",
                    }
                }
            }
        )
    )
    assert load_strategy_book().get("mine").cascade is not None
    provider = Scripted({FAST: AGREED, SECURITY: OTHER, WEAK: UNRELATED})
    result = await pipeline(tmp_path, provider).run(context(strategy="mine"))
    assert result.cascade is not None and not result.cascade.exited_early
    assert result.final_answer.startswith("## Panel digest: 3 answers")  # escalated: the digest


# ---------------------------------------------------------------------- tools and the command line


async def test_a_cascade_runs_through_the_mcp_tool(tmp_path: Path) -> None:
    from fastmcp import Client

    from fusion.mcp_server.server import create_mcp_server

    server = create_mcp_server(db_path=str(tmp_path / "runs.db"))
    async with Client(server) as client:
        arguments = {
            "prompt": "How do I retry an HTTP call?",
            "strategy": "panel-cascade",
            "detail": "full",
        }
        result = await client.call_tool("fusion_ask", {"input": arguments})
    routing = result.structured_content["routing"]
    assert routing["strategy"] == "panel-cascade"
    assert any(r.startswith("Cascade:") for r in routing["reasons"])


def test_the_command_line_can_run_a_cascade_and_list_it(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from fusion.cli.app import app

    ran = CliRunner().invoke(
        app, ["run-mock", "--strategy", "panel-cascade", "--db-path", str(tmp_path / "c.db")]
    )
    assert ran.exit_code == 0, ran.output
    listed = CliRunner().invoke(app, ["strategies", "list", "--json"])
    rows = {row["name"]: row for row in yaml.safe_load(listed.output)}
    assert rows["panel-cascade"]["kind"] == "cascade" and rows["panel-vote"]["aggregator"] == "vote"
