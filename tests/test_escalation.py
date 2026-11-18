"""strategy: auto: estimate difficulty, start on a rung of the ladder, climb only when needed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from _scripted import (
    AGREED,
    FAST,
    OTHER,
    SECURITY,
    WEAK,
    Scripted,
    context,
    pipeline,
    stages,
)
from fusion.config.layers import ConfigError
from fusion.config.loader import EscalationPolicy, Rung, load_routing_policies
from fusion.orchestration.context import PipelineContext
from fusion.orchestration.escalate import EscalationOutcome, climbable_rungs
from fusion.orchestration.strategy import Strategy, load_strategy_book
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.routing.classifier import TaskType
from fusion.routing.policy import build_routing

EASY = "Is `len([])` equal to 0 in Python?"
NORMAL = (
    "Should we retry failed HTTP calls in our async worker with exponential backoff, and what "
    "are the trade-offs for the database writes it makes?"
)


def _answer(*texts: str, confidence: float = 0.7, kind: str = "recommendation") -> str:
    return json.dumps(
        {
            "summary": "summary",
            "claims": [
                {
                    "id": f"c{i}",
                    "text": text,
                    "kind": kind,
                    "severity": None,
                    "file": None,
                    "line": None,
                    "evidence": None,
                }
                for i, text in enumerate(texts, 1)
            ],
            "confidence": confidence,
            "score": None,
        }
    )


TRACE = (
    'Traceback (most recent call last):\n  File "app/handlers.py", line 12, in get_user\n'
    "    return session['user']\nKeyError: 'user'\nThe handler fails for logged-out visitors."
)
UNSURE = _answer("Probably yes, but check the docs", confidence=0.3)


def _ladder(monkeypatch: pytest.MonkeyPatch, rungs: str, start: str) -> None:
    monkeypatch.setenv("FUSION__ROUTING__ESCALATION__RUNGS", rungs)
    monkeypatch.setenv("FUSION__ROUTING__ESCALATION__START_BY_LEVEL", start)


def _climb(result: Any) -> EscalationOutcome:
    assert isinstance(result.escalation, EscalationOutcome)
    return result.escalation


# ---------------------------------------------------------------------------------- the config


def test_the_packaged_ladder_is_nano_solo_nano_panel_mid_panel_then_frontier() -> None:
    policy = load_routing_policies().routing.escalation
    assert [(r.name, r.strategy, r.frontier) for r in policy.rungs] == [
        ("nano-solo", "solo-haiku-check", False),
        ("nano-panel", "panel-nano-auto", False),
        ("mid-panel", "panel-mid", False),
        ("frontier", "solo-opus", True),
    ]
    assert policy.start_by_level == {"easy": 0, "normal": 1, "hard": 2}
    assert policy.allow_frontier is False


def test_auto_is_the_default_strategy_and_a_frontier_variant_opts_in() -> None:
    book = load_strategy_book()
    assert book.for_budget("medium").name == "auto"
    auto, frontier = book.get("auto"), book.get("auto-frontier")
    assert auto.kind == frontier.kind == "auto" and auto.members == []
    assert frontier.escalation is not None and frontier.escalation.allow_frontier is True
    rung0 = book.get("solo-haiku-check")
    assert rung0.kind == "solo" and rung0.self_check


def test_the_frontier_rung_is_climbable_only_when_allowed() -> None:
    policy = load_routing_policies().routing.escalation
    assert climbable_rungs(policy) == [0, 1, 2]
    allowed = policy.model_copy(update={"allow_frontier": True})
    assert climbable_rungs(allowed) == [0, 1, 2, 3]


def test_an_auto_strategy_takes_no_members_and_others_need_one() -> None:
    with pytest.raises(ValueError, match="auto"):
        Strategy(name="x", kind="auto", members=[{"model": "claude-haiku"}])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="member"):
        Strategy(name="y", kind="panel", members=[])


def test_a_rung_must_name_a_known_solo_or_panel_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    _ladder(monkeypatch, "[{name: a, strategy: no-such-strategy}]", "{easy: 0, normal: 0, hard: 0}")
    with pytest.raises(ConfigError, match="no-such-strategy"):
        build_routing(use_mock=True)
    _ladder(monkeypatch, "[{name: a, strategy: panel-cascade}]", "{easy: 0, normal: 0, hard: 0}")
    with pytest.raises(ConfigError, match="cascade"):
        build_routing(use_mock=True)


def test_start_levels_must_point_at_a_rung() -> None:
    with pytest.raises(ValueError, match="past"):
        EscalationPolicy(rungs=[Rung(name="a", strategy="solo-haiku")])


# ----------------------------------------------------------------------------- the pipeline


async def test_an_easy_request_makes_one_model_call(tmp_path: Path) -> None:
    provider = Scripted()
    result = await pipeline(tmp_path, provider).run(context(EASY))
    assert stages(result) == ["panel"]  # no classifier call, no aggregator
    climb = _climb(result)
    assert climb.difficulty.level == "easy" and climb.difficulty.source == "signals"
    assert climb.start_rung == climb.answered_at == 0
    assert [a.strategy for a in climb.rungs] == ["solo-haiku-check"]
    assert result.routing.escalation.startswith("answered at rung 0 (nano-solo): self-check")
    # the rung-0 member was asked for a self-check
    assert "Self-check" in provider.requests[0].user_prompt


async def test_low_self_confidence_climbs_one_rung_and_reuses_the_answer(tmp_path: Path) -> None:
    provider = Scripted({FAST: UNSURE})
    result = await pipeline(tmp_path, provider).run(context(EASY))
    climb = _climb(result)
    assert [a.rung for a in climb.rungs] == [0, 1]
    assert climb.rungs[0].trigger == "low_confidence"
    assert "0.30" in climb.rungs[0].reason
    assert climb.rungs[1].reused == [FAST] and sorted(climb.rungs[1].asked) == [SECURITY, WEAK]
    assert sorted(provider.models_called("panel")) == [FAST, SECURITY, WEAK]  # FAST asked once
    assert climb.answered_at == 1
    assert result.agreement is not None and result.agreement.n_models == 3


async def test_low_agreement_escalates_exactly_one_rung_and_reuses_the_lower_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ladder(
        monkeypatch,
        "[{name: duo, strategy: panel-duo}, {name: trio, strategy: panel-nano}]",
        "{easy: 0, normal: 0, hard: 0}",
    )
    provider = Scripted({FAST: AGREED, SECURITY: OTHER, WEAK: AGREED})
    result = await pipeline(tmp_path, provider).run(context(EASY))
    climb = _climb(result)
    assert [a.name for a in climb.rungs] == ["duo", "trio"]
    assert climb.rungs[0].trigger == "low_agreement"
    assert climb.rungs[1].reused == [FAST, SECURITY] and climb.rungs[1].asked == [WEAK]
    assert sorted(provider.models_called("panel")) == [FAST, SECURITY, WEAK]
    # the duo was not merged: escalating needed no synthesis, the trio's run made the one call
    assert stages(result).count("synthesis") == 1
    assert result.agreement is not None and result.agreement.n_models == 3
    assert climb.answered_at == 1


async def test_a_cost_cap_too_small_for_the_next_rung_returns_the_best_answer_so_far(
    tmp_path: Path,
) -> None:
    provider = Scripted({FAST: UNSURE})
    result = await pipeline(tmp_path, provider, priced=True).run(context(EASY, max_cost_usd=0.02))
    climb = _climb(result)
    assert [a.rung for a in climb.rungs] == [0]
    assert climb.rungs[0].trigger == "low_confidence"
    assert climb.answered_at == 0 and climb.stopped and "Cost cap" in climb.stopped
    assert provider.models_called("panel") == [FAST]
    assert any("best answer so far" in w for w in result.warnings)
    assert "check the docs" in result.final_answer


async def test_the_frontier_rung_is_never_used_unless_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FUSION__ROUTING__ESCALATION__START_BY_LEVEL", "{easy: 3, normal: 3, hard: 3}"
    )
    disagreeing = {FAST: AGREED, SECURITY: OTHER, WEAK: _answer("Unrelated point")}
    result = await pipeline(tmp_path, Scripted(disagreeing)).run(context(EASY))
    climb = _climb(result)
    assert climb.start_rung == 2  # the frontier start was moved down
    assert [a.rung for a in climb.rungs] == [2]
    assert climb.stopped and "allow_frontier" in climb.stopped
    monkeypatch.setenv("FUSION__ROUTING__ESCALATION__ALLOW_FRONTIER", "true")
    opted = await pipeline(tmp_path, Scripted(disagreeing)).run(context(EASY))
    assert _climb(opted).start_rung == 3


async def test_the_auto_frontier_strategy_climbs_to_rung_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ladder(
        monkeypatch,
        "[{name: duo, strategy: panel-duo}, {name: top, strategy: panel-nano, frontier: true}]",
        "{easy: 0, normal: 0, hard: 0}",
    )
    provider = Scripted({FAST: AGREED, SECURITY: OTHER, WEAK: AGREED})
    off = await pipeline(tmp_path, provider).run(context(EASY))
    assert [a.name for a in _climb(off).rungs] == ["duo"]
    on = await pipeline(tmp_path, provider).run(context(EASY, strategy="auto-frontier"))
    assert [a.name for a in _climb(on).rungs] == ["duo", "top"]


async def test_ambiguous_signals_make_exactly_one_classifier_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__ROUTING__DIFFICULTY__AMBIGUITY_MARGIN", "0.49")
    provider = Scripted()
    result = await pipeline(tmp_path, provider).run(context(EASY))
    assert len(provider.models_called("difficulty")) == 1
    assert stages(result).count("route") == 1
    climb = _climb(result)
    assert climb.difficulty.source == "classifier"
    assert climb.classifier_cost_usd is not None


async def test_a_clear_estimate_makes_no_classifier_call(tmp_path: Path) -> None:
    provider = Scripted()
    await pipeline(tmp_path, provider).run(context(EASY))
    assert provider.models_called("difficulty") == []


class _Refusing(Scripted):
    """FAST's calls fail with an error no same-tier fallback can cure (a rejected request)."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.model_id == FAST and request.metadata.get("role") == "panel":
            self.requests.append(request)
            return ModelResponse(
                provider="mock", model=FAST, error="rejected", error_type="InvalidRequest"
            )
        return await super().complete(request)


async def test_a_lost_quorum_on_the_start_rung_escalates_instead_of_halting(
    tmp_path: Path,
) -> None:
    provider = _Refusing()
    result = await pipeline(tmp_path, provider).run(context(EASY))
    climb = _climb(result)
    assert climb.rungs[0].trigger == "no_quorum"
    assert climb.answered_at == 1 and result.halt_reason is None


class _Rejecting(Scripted):
    """The selector finds no candidate acceptable."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("role") == "selector":
            self.requests.append(request)
            verdict = {
                "why": "Every candidate invents an API.",
                "acceptable": False,
                "choice": "c1",
                "runner_up": None,
                "missing": [],
            }
            return ModelResponse(provider="mock", model=request.model_id, text=json.dumps(verdict))
        return await super().complete(request)


async def test_a_selector_that_accepts_no_candidate_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ladder(
        monkeypatch,
        "[{name: pick, strategy: panel-nano-select}, {name: mid, strategy: panel-mid}]",
        "{easy: 0, normal: 0, hard: 0}",
    )
    result = await pipeline(tmp_path, _Rejecting()).run(context(EASY))
    climb = _climb(result)
    assert climb.rungs[0].trigger == "none_acceptable"
    assert "invents" in climb.rungs[0].reason


async def test_a_union_with_no_backed_finding_on_a_debugging_task_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ladder(
        monkeypatch,
        "[{name: lenses, strategy: panel-nano-union}, {name: mid, strategy: panel-mid}]",
        "{easy: 0, normal: 0, hard: 0}",
    )
    guess = _answer("The cache is probably stale", kind="hypothesis")
    provider = Scripted({FAST: guess, SECURITY: guess, WEAK: guess})
    ctx = PipelineContext(task_type=TaskType.DEBUGGING, primary_content=TRACE)
    result = await pipeline(tmp_path, provider).run(ctx)
    climb = _climb(result)
    assert climb.rungs[0].trigger == "no_backed_finding"


async def test_the_run_record_lists_rungs_triggers_and_per_rung_cost(tmp_path: Path) -> None:
    provider = Scripted({FAST: UNSURE}, cost={"*": 0.001})
    runner = pipeline(tmp_path, provider)
    result = await runner.run(context(EASY))
    climb = _climb(result)
    total = sum(a.cost_usd for a in climb.rungs) + (climb.classifier_cost_usd or 0.0)
    assert total == pytest.approx(result.total_cost_usd)
    assert all(a.cost_usd > 0 and a.latency_ms >= 0 for a in climb.rungs)
    stored = runner._run_store.get_run(result.run_id)
    record = stored.output_data["escalation"]
    assert [r["trigger"] for r in record["rungs"]] == ["low_confidence", None]
    assert record["difficulty"]["level"] == "easy"
    assert record["summary"].startswith("answered at rung 1")


async def test_an_explicit_strategy_does_not_climb(tmp_path: Path) -> None:
    result = await pipeline(tmp_path, Scripted({FAST: UNSURE})).run(
        context(EASY, strategy="solo-haiku")
    )
    assert result.escalation is None and result.routing.escalation == ""


# --------------------------------------------------------------------------- what the user sees


def test_fusion_ask_mock_with_no_strategy_runs_auto_and_shows_the_rung() -> None:
    from typer.testing import CliRunner

    from fusion.cli.main import app

    runner = CliRunner()
    shown = runner.invoke(app, ["ask", EASY, "--mock"])
    assert shown.exit_code == 0, shown.output
    assert "### Route" in shown.stdout and "answered at rung 0 (nano-solo)" in shown.stdout
    data = json.loads(runner.invoke(app, ["ask", EASY, "--mock", "--json"]).stdout)
    assert data["strategy"] == "solo-haiku-check"
    assert data["escalation"].startswith("answered at rung 0 (nano-solo): self-check")


async def test_a_rung_that_would_pass_the_soft_time_limit_is_not_started(tmp_path: Path) -> None:
    provider = Scripted({FAST: UNSURE}, delay={FAST: 0.3})
    runner = pipeline(tmp_path, provider)
    runner.soft_timeout_s = 0.5  # rung 1 needs a panel and a selector: about 2 x 0.3 s more
    result = await runner.run(context(EASY))
    climb = _climb(result)
    assert [a.rung for a in climb.rungs] == [0]
    assert climb.stopped and climb.stopped.startswith("Soft time limit: rung 1")
    assert provider.models_called("panel") == [FAST] and not result.partial


# ------------------------------------------------------------------------------------- bench


def test_a_bench_plan_forecasts_an_auto_arm_from_its_start_rung_up() -> None:
    from fusion.bench.plan import estimate_job
    from fusion.bench.scoring import get_scorer as scorer_for
    from fusion.bench.spec import BenchTask
    from fusion.config.catalog import load_catalog
    from fusion.routing.model_registry import ModelRegistry
    from fusion.telemetry.cost import PricingRegistry

    routing = build_routing(use_mock=False)
    task = BenchTask(id="t", category="debugging", prompt="KeyError: 'k' in app.py", truth={})
    kw: dict[str, Any] = {
        "routing": routing,
        "registry": ModelRegistry.for_mode(use_mock=False),
        "pricing": PricingRegistry(load_catalog()),
        "scorer": scorer_for(task.category),
    }
    book = routing.strategies
    auto = estimate_job(task, book.get("auto"), **kw)
    rungs = [estimate_job(task, s, **kw) for s in routing.ladder(book.get("auto"), task.task_type)]
    assert auto.low_usd == rungs[0].low_usd
    assert auto.worst_usd == pytest.approx(
        sum(r.worst_usd for r in rungs) - (len(rungs) - 1) * rungs[0].eval_usd
    )
    assert rungs[0].expected_usd <= auto.expected_usd < auto.worst_usd
