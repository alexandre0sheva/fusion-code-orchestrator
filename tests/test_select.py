"""Approaches and the ``select`` aggregator (roadmap Task 8).

Where every member gives a *complete* answer (plans, architecture decisions, asks, patches) the
answers are rivals: each member gets an approach so they differ, and one cheap call picks an id
instead of a synthesizer rewriting everything. Everything runs offline on the mock provider.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from fusion.config.layers import ConfigError
from fusion.orchestration.aggregate import default_aggregator, resolve_aggregator
from fusion.orchestration.approaches import (
    ApproachBook,
    assign_approaches,
    load_approach_book,
)
from fusion.orchestration.budget_guard import plan_calls
from fusion.orchestration.context import PipelineContext
from fusion.orchestration.factory import Settings, build_pipeline
from fusion.orchestration.lenses import base_alias
from fusion.orchestration.prompts import (
    approach_instruction,
    build_refinement_prompt,
    build_select_prompt,
)
from fusion.orchestration.select import (
    anonymise,
    parse_selection,
    render_selection,
)
from fusion.orchestration.strategy import PanelMember, Strategy, load_strategy_book
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.providers.mock import MockProvider
from fusion.routing.budget import SELECT_OUTPUT_TOKENS
from fusion.routing.classifier import TaskType

PLAN_REQUEST = (
    "Add rate limiting to the public API. We run three app servers behind a load balancer."
)


def _members(*aliases: str) -> list[PanelMember]:
    return [PanelMember(model=a) for a in aliases]


def _book() -> ApproachBook:
    return load_approach_book()


def _strategy(**kw: Any) -> Strategy:
    base: dict[str, Any] = {
        "name": "s",
        "kind": "panel",
        "members": [{"model": "claude-haiku"}, {"model": "gpt-luna"}],
    }
    return Strategy.model_validate({**base, **kw})


# ------------------------------------------------------------------------------- the approach set


def test_the_packaged_approach_sets_follow_the_roadmap() -> None:
    book = _book()
    assert [a.name for a in book.for_task("implementation_plan")] == [
        "smallest_change",
        "cleanest_design",
        "safest_rollout",
    ]
    assert len(book.for_task("architecture_decision")) == 3
    assert len(book.for_task("default")) >= 2  # fusion_ask
    assert [a.name for a in book.for_patch()] == ["minimal_fix", "root_cause_fix"]
    # review and debugging keep lenses + union: no approaches there
    assert book.for_task("code_review") == [] and book.for_task("debugging") == []


def test_approach_config_rejects_unknown_task_types(tmp_path: Path) -> None:
    path = tmp_path / "a.yaml"
    path.write_text(
        "approaches:\n  x:\n    task_types: [code_review]\n    instruction: do it\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="code_review"):
        load_approach_book(path)


def test_each_member_gets_a_different_approach_in_order() -> None:
    got = assign_approaches(
        TaskType.IMPLEMENTATION_PLAN, _members("a", "b", "c"), book=_book()
    )
    assert [got[m].name for m in ("a", "b", "c")] == [
        "smallest_change",
        "cleanest_design",
        "safest_rollout",
    ]


def test_approaches_repeat_when_there_are_more_members_and_a_member_may_name_one() -> None:
    members = [PanelMember(model="a", approach="safest_rollout"), *_members("b", "c", "d")]
    got = assign_approaches(TaskType.IMPLEMENTATION_PLAN, members, book=_book())
    assert got["a"].name == "safest_rollout"
    assert got["b"].name == "smallest_change" and got["c"].name == "cleanest_design"
    assert got["d"].name  # cycles


def test_a_patch_task_uses_the_patch_approaches_whatever_its_type() -> None:
    got = assign_approaches(TaskType.DEBUGGING, _members("a", "b"), patch=True, book=_book())
    assert [got[m].name for m in ("a", "b")] == ["minimal_fix", "root_cause_fix"]


def test_one_member_or_no_approaches_means_none() -> None:
    assert assign_approaches(TaskType.IMPLEMENTATION_PLAN, _members("a"), book=_book()) == {}
    assert assign_approaches(TaskType.CODE_REVIEW, _members("a", "b"), book=_book()) == {}


def test_seats_of_one_model_never_repeat_an_approach() -> None:
    members = [PanelMember(model="x"), PanelMember(model="x#2"), PanelMember(model="x#3")]
    got = assign_approaches(TaskType.IMPLEMENTATION_PLAN, members, book=_book())
    assert {base_alias(m) for m in got} == {"x"}
    assert len({a.name for a in got.values()}) == len(got) == 3


def test_the_approach_prompt_gives_a_stance_and_still_demands_a_whole_answer() -> None:
    approach = _book().for_task("implementation_plan")[0]
    text = approach_instruction(approach)
    assert "## Your Approach: smallest_change" in text
    assert approach.instruction.strip() in text
    assert "complete answer" in text  # rivals, not pieces


def test_refinement_keeps_an_approach() -> None:
    approach = _book().for_task("implementation_plan")[2]
    prompt = build_refinement_prompt(
        task_type=TaskType.IMPLEMENTATION_PLAN,
        original_task="t",
        own_answer="mine",
        peer_answers=[("A", "theirs")],
        approach=approach,
    )
    assert "Your Approach: safest_rollout" in prompt


# --------------------------------------------------------------------- anonymising the candidates

ANSWERS = [
    ("claude-haiku", "Use a token bucket in Redis, shared by all three servers."),
    ("gpt-luna", "Rate limit in the load balancer, no code change."),
    ("gemini-flash", "Per-process counters; accept some over-admission."),
]


def test_candidates_are_shuffled_by_run_id_and_stable_for_one_run() -> None:
    first = anonymise(ANSWERS, "run-1")
    again = anonymise(ANSWERS, "run-1")
    assert [(c.id, c.model) for c in first] == [(c.id, c.model) for c in again]
    assert [c.id for c in first] == ["c1", "c2", "c3"]
    orders = {tuple(c.model for c in anonymise(ANSWERS, f"run-{i}")) for i in range(12)}
    assert len(orders) > 1  # the order depends on the run id, so position is not a tell


def test_candidate_text_never_names_a_model() -> None:
    answers = [("gpt-luna", "As gpt-luna (model gpt-6-luna) I would use Redis.")]
    answers.append(("claude-haiku", "Use the load balancer."))
    candidates = anonymise(
        answers, "r", hide=["gpt-luna", "gpt-6-luna", "claude-haiku", "claude-haiku-5-5"]
    )
    joined = " ".join(c.text for c in candidates)
    assert "gpt-luna" not in joined and "gpt-6-luna" not in joined


# --------------------------------------------------------------------------- the selector prompt


def _prompt(**kw: Any) -> str:
    candidates = anonymise(ANSWERS, "run-1")
    base: dict[str, Any] = {
        "task_type": TaskType.IMPLEMENTATION_PLAN,
        "material": PLAN_REQUEST,
        "candidates": [(c.id, c.text) for c in candidates],
    }
    return build_select_prompt(**{**base, **kw})


def test_the_select_prompt_orders_the_criteria_and_says_length_is_not_merit() -> None:
    text = _prompt()
    order = [
        "delivery",
        "conformance",
        "fidelity",
        "consistency",
        "fitness",
        "finish",
    ]
    positions = [text.lower().index(name) for name in order]
    assert positions == sorted(positions)
    assert "length" in text.lower() and "not merit" in text.lower()
    assert "silent" in text.lower()  # prefer the candidate that says the source is silent
    assert PLAN_REQUEST in text
    for cid in ("c1", "c2", "c3"):
        assert f"## Candidate {cid}" in text


def test_the_select_prompt_asks_for_missing_points_only_for_select_plus_patch() -> None:
    assert "missing" not in _prompt(patch_points=0).lower().split("## output")[-1]
    with_points = _prompt(patch_points=3)
    assert "missing" in with_points.lower().split("## output")[-1]
    assert "3" in with_points


def test_the_select_prompt_never_shows_a_model_or_an_approach() -> None:
    text = _prompt()
    for name in ("claude", "haiku", "gpt", "luna", "gemini", "smallest_change"):
        assert name not in text.lower()


# ----------------------------------------------------------------------------- reading the choice


def _resp(text: str, parsed: dict[str, Any] | None = None) -> ModelResponse:
    return ModelResponse(provider="mock", model="m", text=text, parsed_json=parsed)


def test_a_json_choice_is_read() -> None:
    body = {
        "why": "Matches the request.",
        "choice": "c2",
        "runner_up": "c1",
        "missing": [{"from": "c1", "point": "Mention the Redis outage fallback"}],
    }
    got = parse_selection(_resp(json.dumps(body), body), ["c1", "c2", "c3"], max_points=3)
    assert got is not None
    assert (got.choice_id, got.runner_up, got.why) == ("c2", "c1", "Matches the request.")
    assert [(m.source, m.point) for m in got.missing] == [
        ("c1", "Mention the Redis outage fallback")
    ]


def test_a_fenced_choice_in_prose_is_read() -> None:
    text = "c2 is complete and grounded.\nc1 invents a Redis cluster.\n\n```choice\nc2\n```"
    got = parse_selection(_resp(text), ["c1", "c2", "c3"], max_points=0)
    assert got is not None and got.choice_id == "c2"
    assert "grounded" in got.why


@pytest.mark.parametrize(
    "text",
    ["", "no idea", '{"why": "x", "choice": "c9"}', '{"why": "x"}', "```choice\nc7\n```"],
)
def test_a_malformed_or_unknown_choice_is_not_accepted(text: str) -> None:
    assert parse_selection(_resp(text), ["c1", "c2", "c3"], max_points=0) is None


def test_missing_points_are_capped_and_must_name_another_candidate() -> None:
    body = {
        "why": "ok",
        "choice": "c1",
        "runner_up": None,
        "missing": [
            {"from": "c1", "point": "from the winner itself"},
            {"from": "c9", "point": "from nobody"},
            {"from": "c2", "point": "one"},
            {"from": "c3", "point": "two"},
            {"from": "c2", "point": "three"},
            {"from": "c3", "point": "four"},
        ],
    }
    got = parse_selection(_resp(json.dumps(body), body), ["c1", "c2", "c3"], max_points=3)
    assert got is not None
    assert [m.point for m in got.missing] == ["one", "two", "three"]
    none = parse_selection(_resp(json.dumps(body), body), ["c1", "c2", "c3"], max_points=0)
    assert none is not None and none.missing == []


def test_the_chosen_answer_is_returned_as_written_with_the_extra_points_after_it() -> None:
    candidates = anonymise(ANSWERS, "run-1")
    chosen = candidates[1]
    body = {
        "why": "w",
        "choice": chosen.id,
        "runner_up": None,
        "missing": [{"from": candidates[0].id, "point": "Fail open if Redis is down"}],
    }
    result = parse_selection(_resp(json.dumps(body), body), [c.id for c in candidates], 3)
    assert result is not None
    text = render_selection(chosen.text, result)
    assert text.startswith(chosen.text)
    assert "Fail open if Redis is down" in text.split(chosen.text, 1)[1]
    assert render_selection(chosen.text, result.model_copy(update={"missing": []})) == chosen.text


# ----------------------------------------------------------------------------- strategy rules


def test_select_strategies_default_to_approaches_not_lenses() -> None:
    s = _strategy(aggregator="select", aggregator_model="claude-haiku")
    assert s.lenses == "off" and s.approaches == "auto"
    assert _strategy(aggregator="select+patch").approaches == "auto"
    assert _strategy(aggregator="union").approaches == "off"
    assert _strategy(aggregator="llm").approaches == "off"


@pytest.mark.parametrize("aggregator", ["select", "select+patch"])
def test_a_strategy_that_combines_lenses_with_select_is_rejected(aggregator: str) -> None:
    with pytest.raises(ValueError, match="picks ONE whole answer"):
        _strategy(aggregator=aggregator, lenses="auto")
    with pytest.raises(ValueError, match="picks ONE whole answer"):
        _strategy(
            aggregator=aggregator,
            members=[{"model": "claude-haiku", "lens": "security"}, {"model": "gpt-luna"}],
        )


def test_approaches_need_a_select_aggregator() -> None:
    with pytest.raises(ValueError, match="approaches"):
        _strategy(aggregator="union", approaches="auto")
    with pytest.raises(ValueError, match="approaches"):
        _strategy(members=[{"model": "gpt-luna", "approach": "minimal_fix"}, {"model": "x"}])


def test_a_self_mixture_is_allowed_with_approaches() -> None:
    s = _strategy(
        aggregator="select",
        members=[{"model": "gpt-luna"}, {"model": "gpt-luna"}, {"model": "gpt-luna"}],
    )
    assert s.approaches == "auto"
    with pytest.raises(ValueError, match="only with lenses or approaches"):
        _strategy(
            aggregator="select",
            approaches="off",
            members=[{"model": "gpt-luna"}, {"model": "gpt-luna"}],
        )


def test_select_calls_a_model_and_union_does_not() -> None:
    assert _strategy(aggregator="select").calls_aggregator_model
    assert _strategy(aggregator="select+patch").calls_aggregator_model
    assert not _strategy(aggregator="union").calls_aggregator_model


def test_skip_aggregator_and_judge_synthesis_need_llm_not_select() -> None:
    skip = {"agreement_threshold": 0.7, "use": "vote"}
    with pytest.raises(ValueError, match="skip_aggregator needs an llm"):
        _strategy(aggregator="select", skip_aggregator=skip)


def test_a_member_naming_an_unknown_approach_is_rejected_at_load() -> None:
    from fusion.orchestration import strategy as strategy_module

    book = load_strategy_book()
    nano = book.get("panel-nano-select")
    broken = nano.model_copy(
        update={"members": [PanelMember(model="claude-haiku", approach="vibes"), *nano.members[1:]]}
    )
    bad = strategy_module.StrategyBook(
        {"panel-nano-select": broken}, {}, None, book.lenses, book.approaches
    )
    with pytest.raises(ConfigError, match="member claude-haiku: Unknown approach 'vibes'"):
        strategy_module._check_approaches(bad)


# ----------------------------------------------------------------------------- aggregator: auto


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        (TaskType.CODE_REVIEW, "union"),
        (TaskType.DEBUGGING, "union"),
        (TaskType.IMPLEMENTATION_PLAN, "select+patch"),
        (TaskType.ARCHITECTURE_DECISION, "select+patch"),
        (TaskType.DEFAULT, "select+patch"),
    ],
)
def test_auto_picks_union_for_review_and_debug_and_select_for_the_rest(
    task: TaskType, expected: str
) -> None:
    assert default_aggregator(task) == expected


def test_a_patch_task_resolves_to_select_whatever_its_type() -> None:
    # roadmap: coding patches -> verified-synth, which arrives with Task 11
    assert default_aggregator(TaskType.DEBUGGING, patch=True) == "select+patch"


def test_auto_is_resolved_into_a_concrete_strategy_per_task() -> None:
    auto = _strategy(aggregator="auto", aggregator_model="claude-haiku")
    assert auto.lenses == "auto"  # it will lens a review
    review = resolve_aggregator(auto, TaskType.CODE_REVIEW)
    assert (review.aggregator, review.lenses, review.approaches) == ("union", "auto", "off")
    plan = resolve_aggregator(auto, TaskType.IMPLEMENTATION_PLAN)
    assert (plan.aggregator, plan.lenses, plan.approaches) == ("select+patch", "off", "auto")
    # a concrete strategy is left alone
    llm = _strategy(aggregator="llm")
    assert resolve_aggregator(llm, TaskType.IMPLEMENTATION_PLAN) is llm


def test_an_auto_strategy_is_forecast_at_its_dearest_form() -> None:
    auto = _strategy(aggregator="auto", aggregator_model="claude-haiku")
    assert auto.calls_aggregator_model


def test_select_plans_one_small_call_over_every_answer() -> None:
    strat = _strategy(aggregator="select", aggregator_model="claude-haiku")
    calls = plan_calls(strat, judge_model="gpt-luna", prompt_tokens=1_000)
    synthesis = [c for c in calls if c.stage == "synthesis"]
    assert len(synthesis) == 1 and synthesis[0].output_tokens == SELECT_OUTPUT_TOKENS <= 400
    llm = plan_calls(
        _strategy(aggregator_model="claude-haiku"), judge_model="gpt-luna", prompt_tokens=1_000
    )
    assert next(c for c in llm if c.stage == "synthesis").output_tokens > SELECT_OUTPUT_TOKENS


# ------------------------------------------------------------------------- end to end (mock)


class Spy(MockProvider):
    """Records every request by role; the selector can be scripted."""

    def __init__(self, selector: str | None = None) -> None:
        super().__init__(latency_ms=0)
        self.requests: dict[str, list[ModelRequest]] = defaultdict(list)
        self._scripted = selector

    async def complete(self, request: ModelRequest) -> ModelResponse:
        role = str(request.metadata.get("role"))
        self.requests[role].append(request)
        if role == "selector" and self._scripted is not None:
            return ModelResponse(
                provider="mock", model=request.model_id, text=self._scripted, output_tokens=5
            )
        return await super().complete(request)


def _pipe(tmp_path: Path, provider: MockProvider | None = None) -> Any:
    settings = Settings(use_mock=True, db_path=str(tmp_path / "s.db"))
    return build_pipeline(settings, {"mock": provider or MockProvider(latency_ms=0)})


def _plan(**kw: Any) -> PipelineContext:
    return PipelineContext(
        task_type=TaskType.IMPLEMENTATION_PLAN, primary_content=PLAN_REQUEST, **kw
    )


async def test_panel_nano_select_runs_end_to_end_on_the_mock_provider(tmp_path: Path) -> None:
    spy = Spy()
    result = await _pipe(tmp_path, spy).run(_plan(strategy="panel-nano-select"))
    assert result.final_answer
    assert len(spy.requests["panel"]) == 3
    assert len(spy.requests["selector"]) == 1  # one cheap call, no synthesizer
    assert "synthesizer" not in spy.requests
    selection = result.disagreement["selection"]
    assert selection["choice"] in {"c1", "c2", "c3"} and selection["model"]
    assert not selection.get("fallback")
    picked = [r for r in result.ledger.records if r.stage == "synthesis"]
    assert len(picked) == 1  # the selector's cost is in the ledger, next to the panel's
    assert any("select picked" in r for r in result.routing.reasons)


async def test_every_member_gets_its_own_approach_after_the_shared_prefix(tmp_path: Path) -> None:
    spy = Spy()
    result = await _pipe(tmp_path, spy).run(_plan(strategy="panel-nano-select"))
    prompts = [r.user_prompt for r in spy.requests["panel"]]
    assert len({p.split("## Your Approach:")[0] for p in prompts}) == 1  # one shared prefix
    names = sorted(p.split("## Your Approach: ")[1].split("\n")[0] for p in prompts)
    assert names == ["cleanest_design", "safest_rollout", "smallest_change"]
    assert sorted(result.disagreement["approaches"].values()) == names
    assert all("## Your Lens" not in p for p in prompts)  # rivals, not pieces


async def test_the_selector_sees_anonymous_shuffled_candidates_only(tmp_path: Path) -> None:
    spy = Spy()
    await _pipe(tmp_path, spy).run(_plan(strategy="panel-nano-select"))
    (request,) = spy.requests["selector"]
    prompt = request.user_prompt + request.system_prompt
    assert prompt.count("## Candidate c") == 3
    for alias in ("mock-fast", "mock-strong", "mock-cheap", "claude", "gpt", "gemini"):
        assert alias not in prompt.lower()
    for name in ("smallest_change", "cleanest_design", "safest_rollout"):
        assert name not in prompt  # the stance a member was given is not shown either
    assert request.max_tokens <= SELECT_OUTPUT_TOKENS


async def test_a_malformed_choice_falls_back_to_a_vote_with_a_warning(tmp_path: Path) -> None:
    spy = Spy(selector="I like the second one best.")
    result = await _pipe(tmp_path, spy).run(_plan(strategy="panel-nano-select"))
    assert result.final_answer  # still an answer
    assert any("no valid choice" in w for w in result.warnings)
    # the mock panel's answers share no claim, so there is no majority to vote on: best_of
    assert result.disagreement["selection"]["fallback"] in {"vote", "best_of"}
    assert not any("picked" in r and "select" in r for r in result.routing.reasons)


async def test_a_selector_that_names_an_unknown_candidate_also_falls_back(tmp_path: Path) -> None:
    spy = Spy(selector=json.dumps({"why": "x", "choice": "c9", "runner_up": None, "missing": []}))
    result = await _pipe(tmp_path, spy).run(_plan(strategy="panel-nano-select"))
    assert any("no valid choice" in w for w in result.warnings)


async def test_the_chosen_candidate_is_what_comes_back(tmp_path: Path) -> None:
    spy = Spy(selector=json.dumps({"why": "best", "choice": "c2", "runner_up": "c1"}))
    result = await _pipe(tmp_path, spy).run(_plan(strategy="panel-nano-select"))
    (request,) = spy.requests["selector"]
    body = request.user_prompt.split("## Candidate c2")[1].split("## Candidate c")[0]
    summary = next(ln.strip() for ln in body.splitlines() if "mock:" in ln)
    assert summary in result.final_answer  # the chosen answer, as written
    assert result.disagreement["selection"]["choice"] == "c2"
    assert result.disagreement["selection"]["runner_up"] == "c1"


async def test_select_plus_patch_adds_the_points_the_selector_marked(tmp_path: Path) -> None:
    body = {
        "why": "best",
        "choice": "c1",
        "runner_up": None,
        "missing": [{"from": "c2", "point": "Return Retry-After on a 429"}],
    }
    spy = Spy(selector=json.dumps(body))
    result = await _pipe(tmp_path, spy).run(_plan(strategy="panel-nano-select"))
    assert "Return Retry-After on a 429" in result.final_answer
    assert result.disagreement["selection"]["added_points"] == 1


async def test_plain_select_adds_nothing(tmp_path: Path) -> None:
    body = {
        "why": "best",
        "choice": "c1",
        "runner_up": None,
        "missing": [{"from": "c2", "point": "Return Retry-After on a 429"}],
    }
    spy = Spy(selector=json.dumps(body))
    pipe = _pipe(tmp_path, spy)
    book = pipe.deps.routing.strategies
    from fusion.orchestration.strategy import StrategyBook

    plain = book.get("panel-nano-select").model_copy(
        update={"name": "plain", "aggregator": "select"}
    )
    pipe.deps.routing._strategies = StrategyBook(
        {**book.strategies, "plain": plain}, book.budget_map, book.aliases, book.lenses,
        book.approaches,
    )
    result = await pipe.run(_plan(strategy="plain"))
    assert "Retry-After" not in result.final_answer
    assert "missing" not in spy.requests["selector"][0].user_prompt.lower().split("## output")[-1]


async def test_auto_reviews_with_lenses_and_union_but_plans_with_approaches_and_select(
    tmp_path: Path,
) -> None:
    pipe = _pipe(tmp_path)
    book = pipe.deps.routing.strategies
    from fusion.orchestration.strategy import StrategyBook

    assert book.get("panel-nano-auto").aggregator == "auto"
    review = await pipe.run(
        PipelineContext(
            task_type=TaskType.CODE_REVIEW,
            primary_content="diff --git a/x.py b/x.py\n+x = 1\n",
            changed_files=["x.py"],
            strategy="panel-nano-auto",
        )
    )
    assert review.final_answer.startswith("## Panel union:")
    assert "lenses" in review.disagreement and "selection" not in review.disagreement
    assert {r.stage for r in review.ledger.records} == {"panel"}

    plan = await pipe.run(_plan(strategy="panel-nano-auto"))
    assert "selection" in plan.disagreement and "lenses" not in plan.disagreement
    assert plan.disagreement["approaches"]
    assert StrategyBook  # the book itself still holds the unresolved strategy
    assert book.get("panel-nano-auto").aggregator == "auto"


async def test_a_patch_task_asks_for_patch_approaches(tmp_path: Path) -> None:
    spy = Spy()
    await _pipe(tmp_path, spy).run(
        PipelineContext(
            task_type=TaskType.DEBUGGING,
            primary_content="TypeError in parse(): 'NoneType' has no len()",
            strategy="panel-nano-auto",
            expects_patch=True,
        )
    )
    names = {
        r.user_prompt.split("## Your Approach: ")[1].split("\n")[0]
        for r in spy.requests["panel"]
    }
    assert names == {"minimal_fix", "root_cause_fix"}  # not the debugging task's own stances
    assert len(spy.requests["selector"]) == 1


async def test_a_self_mixture_asks_one_model_once_per_approach(tmp_path: Path) -> None:
    spy = Spy()
    result = await _pipe(tmp_path, spy).run(_plan(strategy="panel-luna-approaches"))
    names = [p.model_name for p in result.panel_results]
    assert names == ["mock-fast", "mock-fast#2", "mock-fast#3"]
    assert len({r.model_id for r in result.ledger.records if r.stage == "panel"}) == 1
    assert len(spy.requests["selector"]) == 1


async def test_a_cap_that_leaves_no_room_for_the_selector_falls_back_to_a_vote(
    tmp_path: Path,
) -> None:
    spy = Spy()
    spy._latency_ms = 30.0  # the panel takes 30 ms, so a 20 ms latency cap has no room left
    pipe = _pipe(tmp_path, spy)
    book = pipe.deps.routing.strategies
    from fusion.orchestration.strategy import StrategyBook

    capped = book.get("panel-nano-select").model_copy(
        update={"name": "capped", "max_latency_s": 0.02}
    )
    pipe.deps.routing._strategies = StrategyBook(
        {**book.strategies, "capped": capped},
        book.budget_map,
        book.aliases,
        book.lenses,
        book.approaches,
    )
    result = await pipe.run(_plan(strategy="capped"))
    assert "selector" not in spy.requests  # never asked
    assert any("Latency cap" in w and "chose by vote" in w for w in result.warnings)
    assert result.final_answer
    assert result.disagreement["selection"]["fallback"] in {"vote", "best_of"}


async def test_one_surviving_answer_needs_no_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__FANOUT__MIN_SUCCESSFUL_RESPONSES", "1")

    class OneAnswer(Spy):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("role") == "panel":
                self.requests["panel"].append(request)
                if len(self.requests["panel"]) > 1:
                    raise ConnectionError("down")
                return await MockProvider.complete(self, request)
            return await super().complete(request)

    spy = OneAnswer()
    result = await _pipe(tmp_path, spy).run(_plan(strategy="panel-nano-select"))
    assert len(result.panel_results) == 1
    assert "selector" not in spy.requests  # one answer has no rival
    assert result.final_answer


async def test_the_structured_answer_speaks_for_the_chosen_candidate_alone(
    tmp_path: Path,
) -> None:
    spy = Spy(selector=json.dumps({"why": "best", "choice": "c1", "runner_up": None}))
    result = await _pipe(tmp_path, spy).run(
        PipelineContext(
            task_type=TaskType.DEFAULT,
            primary_content="How should I add rate limiting behind three app servers?",
            strategy="panel-nano-select",
        )
    )
    picked = result.disagreement["selection"]["picked"]
    summaries = [ln for ln in result.final_answer.splitlines() if "(mock:" in ln]
    assert len(summaries) >= 1
    assert result.structured_output["answer"].count("(mock:") == 1  # not every panelist's
    assert picked in {p.model_name for p in result.panel_results}
