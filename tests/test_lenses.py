"""Panel lenses and the union aggregator (roadmap Task 7).

A lens gives each member of a review or debugging panel one area to report on; the answers are
pieces of one result, added together by ``union``, which keeps only findings the input backs.
Everything runs offline on the mock provider or on hand-written answers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from fusion.config.layers import ConfigError
from fusion.config.loader import FanoutConfig, ModelEntry
from fusion.orchestration.claims import (
    Claim,
    PanelAnswer,
    agreement_score,
    cluster_claims,
    panel_answer_schema,
    parse_panel_answer,
)
from fusion.orchestration.context import PipelineContext
from fusion.orchestration.factory import Settings, build_pipeline
from fusion.orchestration.fanout import fanout_to_panel
from fusion.orchestration.ledger import CallGateway, RunLedger
from fusion.orchestration.lenses import (
    LensBook,
    assign_lenses,
    base_alias,
    combine_lenses,
    load_lens_book,
    seat_alias,
    seated,
)
from fusion.orchestration.prompts import build_refinement_prompt, lens_instruction
from fusion.orchestration.strategy import PanelMember, Strategy, load_strategy_book
from fusion.orchestration.union import (
    Material,
    apply_check,
    check_claim,
    merge_findings,
    render_union,
    union_claims,
)
from fusion.providers.base import ModelRequest, ModelResponse, ServerError
from fusion.providers.mock import MockProvider
from fusion.routing.classifier import TaskType
from fusion.routing.model_registry import ModelRegistry
from fusion.telemetry.cost import PricingRegistry

DIFF = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -18,4 +18,6 @@ def find_user(name):
     conn = connect()
-    rows = conn.execute("SELECT * FROM users WHERE name = ?", (name,))
+    query = f"SELECT * FROM users WHERE name = '{name}'"
+    rows = conn.execute(query)
     return rows.fetchall()
"""


def _members(*aliases: str) -> list[PanelMember]:
    return [PanelMember(model=a) for a in aliases]


def _book() -> LensBook:
    return load_lens_book()


# ------------------------------------------------------------------------------- the lens set


def test_the_packaged_lens_sets_follow_the_roadmap() -> None:
    book = _book()
    assert [lens.name for lens in book.for_task(TaskType.CODE_REVIEW)] == [
        "correctness",
        "security",
        "performance",
        "contract",
    ]
    assert [lens.name for lens in book.for_task(TaskType.DEBUGGING)] == [
        "hypotheses",
        "evidence",
        "root_cause",
    ]
    for task in (TaskType.IMPLEMENTATION_PLAN, TaskType.ARCHITECTURE_DECISION):
        assert [lens.name for lens in book.for_task(task)] == ["procedure", "domain"]
    assert book.for_task(TaskType.DEFAULT) == [] and book.for_task(TaskType.ANSWER_EVAL) == []
    # aliases of a task type resolve to its canonical lenses
    assert book.for_task(TaskType.PLANNING) == book.for_task(TaskType.IMPLEMENTATION_PLAN)
    for lens in book.lenses.values():  # Harness² style: each says what it owns and leaves
        assert lens.owns and lens.ignores and lens.instruction


def test_a_lens_config_rejects_unknown_task_types_and_bad_names(tmp_path: Path) -> None:
    bad_type = tmp_path / "a.yaml"
    bad_type.write_text(
        "lenses:\n  x:\n    task_types: [poetry]\n    instruction: i\n    owns: [o]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="poetry"):
        load_lens_book(bad_type)
    bad_name = tmp_path / "b.yaml"
    bad_name.write_text(
        "lenses:\n  a+b:\n    task_types: [debugging]\n    instruction: i\n    owns: [o]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="'\\+'"):
        load_lens_book(bad_name)


def test_combined_lenses_cover_every_area_of_their_parts() -> None:
    book = _book()
    merged = book.get("correctness+contract")
    assert merged.parts == ["correctness", "contract"]
    assert merged.owns == [*book.get("correctness").owns, *book.get("contract").owns]
    assert merged == combine_lenses([book.get("correctness"), book.get("contract")])
    with pytest.raises(ConfigError, match="Unknown lens 'nope'"):
        book.get("correctness+nope")


# --------------------------------------------------------------------------------- assignment


def test_each_member_gets_a_different_lens_in_order() -> None:
    lenses = assign_lenses(TaskType.DEBUGGING, _members("a", "b", "c"), _book())
    assert {k: v.name for k, v in lenses.items()} == {
        "a": "hypotheses",
        "b": "evidence",
        "c": "root_cause",
    }


def test_fewer_members_than_lenses_share_them_so_every_area_is_covered() -> None:
    lenses = assign_lenses(TaskType.CODE_REVIEW, _members("a", "b"), _book())
    assert {k: v.name for k, v in lenses.items()} == {
        "a": "correctness+performance",
        "b": "security+contract",
    }
    covered = {part for lens in lenses.values() for part in lens.parts}
    assert covered == {"correctness", "security", "performance", "contract"}


def test_more_members_than_lenses_repeat_them() -> None:
    lenses = assign_lenses(TaskType.IMPLEMENTATION_PLAN, _members("a", "b", "c"), _book())
    assert [v.name for v in lenses.values()] == ["procedure", "domain", "procedure"]


def test_a_member_that_names_its_lens_keeps_it_and_the_rest_share_the_others() -> None:
    members = [PanelMember(model="a"), PanelMember(model="b", lens="security")]
    lenses = assign_lenses(TaskType.CODE_REVIEW, members, _book())
    assert lenses["b"].name == "security"
    assert lenses["a"].name == "correctness+performance+contract"
    # a lens of another task type does not apply here: the member is assigned like the others
    other = [PanelMember(model="a", lens="security"), PanelMember(model="b")]
    debug = assign_lenses(TaskType.DEBUGGING, other, _book())
    assert {v.name for v in debug.values()} == {"hypotheses+root_cause", "evidence"}


def test_no_lenses_for_one_member_or_a_task_type_without_them() -> None:
    assert assign_lenses(TaskType.CODE_REVIEW, _members("a"), _book()) == {}
    assert assign_lenses(TaskType.DEFAULT, _members("a", "b"), _book()) == {}


def test_seats_of_one_model_never_repeat_a_lens() -> None:
    members = seated(_members("luna", "luna", "luna"))
    assert [m.model for m in members] == ["luna", "luna#2", "luna#3"]
    assert [base_alias(m.model) for m in members] == ["luna"] * 3
    assert seat_alias("luna", 1) == "luna" and seat_alias("luna", 4) == "luna#4"
    review = assign_lenses(TaskType.CODE_REVIEW, members, _book())
    assert [v.name for v in review.values()] == ["correctness+contract", "security", "performance"]
    # plans have two lenses: the third copy would repeat one, so it is not asked
    plan = assign_lenses(TaskType.IMPLEMENTATION_PLAN, members, _book())
    assert list(plan) == ["luna", "luna#2"]


# ------------------------------------------------------------------------------------ prompts


def test_the_lens_prompt_states_its_area_the_boundary_and_that_no_finding_is_valid() -> None:
    book = _book()
    security, correctness = book.get("security"), book.get("correctness")
    text = lens_instruction(security, [correctness, security])
    assert "## Your Lens: security" in text
    assert security.instruction.strip() in text
    assert all(f"- {area}" in text for area in security.owns)
    assert "Leave to others, do not report:" in text
    assert all(f"- {item}" in text for item in security.ignores)
    assert f"{', '.join(correctness.owns)} (correctness lens)" in text  # the others' areas
    assert "(security lens)" not in text  # its own area is not listed as someone else's
    assert "`none_found` to true" in text and "never invent a finding" in text


async def test_every_member_prompt_carries_its_own_lens_after_the_shared_prefix() -> None:
    seen: list[ModelRequest] = []

    class Recording(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            seen.append(request)
            return await super().complete(request)

    registry = ModelRegistry.for_mode(use_mock=True)
    models = registry.models
    lenses = assign_lenses(TaskType.CODE_REVIEW, _members("mock-fast", "mock-security"), _book())
    await fanout_to_panel(
        panel_models=["mock-fast", "mock-security"],
        registry_models=models,
        providers={"mock": Recording(latency_ms=0)},
        task_type=TaskType.CODE_REVIEW,
        primary_content=DIFF,
        file_snippets=["app/db.py\nimport os\n"],
        lenses=lenses,
    )
    by_model = {r.model_id: r for r in seen}
    fast, security = by_model["mock-fast"], by_model["mock-security"]
    assert "## Your Lens: correctness+performance" in fast.user_prompt
    assert "## Your Lens: security+contract" in security.user_prompt
    assert fast.metadata["lens"] == "correctness+performance"
    # the lens replaces the catalog persona (mock-security is a security_reviewer)
    assert "## Your Role" not in security.user_prompt
    shared = fast.shared_prefix_chars
    assert shared and fast.user_prompt[:shared] == security.user_prompt[:shared]


def test_refinement_keeps_a_lensed_member_inside_its_area() -> None:
    lens = _book().get("evidence")
    text = build_refinement_prompt(
        task_type=TaskType.DEBUGGING,
        original_task="boom",
        own_answer="{}",
        peer_answers=[("A", "{}")],
        lens=lens,
    )
    assert "## Your Lens: evidence" in text and "leave the other areas to them" in text


def test_the_claim_rules_ask_for_exact_quotes_and_allow_no_finding() -> None:
    from fusion.orchestration.prompts import build_user_prompt

    text = build_user_prompt(task_type=TaskType.CODE_REVIEW, primary_content=DIFF)
    assert "exact quote" in text and "`none_found`" in text
    assert "none_found" in panel_answer_schema()["properties"]
    assert "none_found" in panel_answer_schema()["required"]


# ----------------------------------------------------------------------------------- none_found


def test_none_found_is_parsed_and_recorded() -> None:
    said, ok = parse_panel_answer('{"summary": "Nothing", "claims": [], "none_found": true}')
    assert ok and said.none_found and said.claims == []
    empty, _ = parse_panel_answer('{"summary": "All good", "claims": []}')
    assert empty.none_found  # no claims at all is also "nothing to report"
    advice, _ = parse_panel_answer(
        '{"summary": "s", "none_found": true, "claims": [{"text": "add a test", "kind": "test"}]}'
    )
    assert advice.none_found and len(advice.claims) == 1  # advice is not a finding
    found, _ = parse_panel_answer(
        '{"summary": "s", "none_found": true, '
        '"claims": [{"text": "bug", "kind": "finding", "severity": "high"}]}'
    )
    assert not found.none_found  # a reported finding wins over the flag
    prose, ok = parse_panel_answer("- one bug\n- another")
    assert not ok and not prose.none_found


# -------------------------------------------------------------------------------- agreement


def _answer(*claims: Claim) -> PanelAnswer:
    return PanelAnswer(summary="s", claims=list(claims))


def _finding(text: str, line: int | None = None, **kw: Any) -> Claim:
    file = "app/db.py" if line is not None else None
    return Claim(text=text, kind="finding", file=file, line=line, **kw)


def test_agreement_does_not_penalise_points_only_one_lens_could_see() -> None:
    shared = _finding("String formatted SQL query allows injection", 20, severity="high")
    answers = {
        "a": _answer(shared, _finding("Off by one in the pagination loop", 5)),
        "b": _answer(shared, _finding("No timeout on the database connect call", 40)),
    }
    clusters = cluster_claims(answers)
    plain = agreement_score(clusters, 2)
    lensed = agreement_score(
        cluster_claims(answers), 2, lenses={"a": "correctness", "b": "performance"}
    )
    assert plain.score == pytest.approx(1 / 3)  # two single-model points drag it down
    assert lensed.score == pytest.approx(1.0)  # they were each lens's own area
    assert len(lensed.lens_exclusive) == 2 and set(lensed.lens_exclusive) <= set(lensed.unique)
    assert lensed.confidence > plain.confidence


def test_a_point_another_holder_of_the_same_lens_missed_still_counts_as_unique() -> None:
    answers = {
        "a": _answer(_finding("Injection through the query", 20)),
        "b": _answer(_finding("Unbounded retry loop", 50)),
        "c": _answer(_finding("Injection through the query", 20)),
    }
    lenses = {"a": "security", "b": "performance", "c": "security"}
    report = agreement_score(cluster_claims(answers), 3, lenses=lenses)
    # b is the only performance lens: its point is lens-exclusive; the security point is shared
    assert len(report.lens_exclusive) == 1
    one_lens = agreement_score(cluster_claims(answers), 3, lenses={m: "x" for m in answers})
    assert one_lens.lens_exclusive == []  # one lens for everyone is no split at all


def test_a_lens_whose_points_all_stand_alone_is_not_an_outlier() -> None:
    answers = {
        "a": _answer(_finding("Injection through the query", 20)),
        "b": _answer(_finding("Injection through the query", 20)),
        "c": _answer(_finding("Unbounded retry loop", 50)),
    }
    assert agreement_score(cluster_claims(answers), 3).outliers == ["c"]
    lensed = agreement_score(
        cluster_claims(answers), 3, lenses={"a": "security", "b": "security", "c": "performance"}
    )
    assert lensed.outliers == []


# ---------------------------------------------------------------------------- evidence check


def _material() -> Material:
    return Material.from_inputs(primary=DIFF, changed_files=["app/db.py"])


def test_a_quote_found_in_the_input_backs_a_finding() -> None:
    claim = _finding("Injection", evidence='query = f"SELECT * FROM users')
    assert check_claim(claim, _material())[0] == "backed"
    # whitespace and case do not matter; an ellipsis joins two pieces that both appear
    loose = _finding("Injection", evidence='QUERY  =  f"select * ... rows.fetchall()')
    assert check_claim(loose, _material())[0] == "backed"


def test_a_quote_that_is_not_in_the_input_is_unbacked() -> None:
    claim = _finding("Injection", evidence="cursor.execute(sql % params)")
    verdict, reason = check_claim(claim, _material())
    assert verdict == "unbacked" and "quote" in reason


def test_a_cited_line_inside_a_diff_hunk_backs_a_finding() -> None:
    assert check_claim(_finding("Injection", 20), _material())[0] == "backed"
    assert check_claim(_finding("Injection", 22), _material())[0] == "backed"  # within the hunk
    far = check_claim(_finding("Injection", 300), _material())
    assert far[0] == "unchecked"  # the file is real, the line is not shown: nothing to confirm


def test_a_file_the_input_never_names_is_unbacked() -> None:
    claim = Claim(text="Leak", kind="finding", file="app/cache.py", line=3)
    verdict, reason = check_claim(claim, _material())
    assert verdict == "unbacked" and "app/cache.py" in reason


def test_a_finding_with_nothing_to_check_is_unchecked() -> None:
    assert check_claim(_finding("No tests cover this change"), _material())[0] == "unchecked"
    # too short to count as a quote
    tiny = _finding("Bad name", evidence="q")
    assert check_claim(tiny, _material())[0] == "unchecked"


def test_stack_trace_frames_and_snippets_count_as_shown_lines() -> None:
    trace = 'Traceback:\n  File "app/pool.py", line 88, in acquire\nTimeoutError'
    material = Material.from_inputs(primary=trace, snippets=["lib/retry.py\na\nb\nc\n"])
    pool = Claim(text="pool exhausted", kind="hypothesis", file="app/pool.py", line=87)
    retry = Claim(text="retry loop", kind="hypothesis", file="lib/retry.py", line=3)
    assert check_claim(pool, material)[0] == "backed"
    assert check_claim(retry, material)[0] == "backed"
    assert material.lines_of("./lib/retry.py") == {1, 2, 3}


# ----------------------------------------------------------------------------------- union


def _review_answers() -> dict[str, PanelAnswer]:
    return {
        "haiku": _answer(
            _finding("SQL injection via the formatted query", 20, severity="high"),
            _finding("Missing check for an empty name", evidence="def find_user(name)"),
            Claim(text="Add a test with a quote in the name", kind="test"),
        ),
        "luna": _answer(
            _finding(
                "String formatted SQL query allows injection",
                19,
                severity="critical",
                evidence="f\"SELECT * FROM users WHERE name = '{name}'\"",
            ),
            Claim(
                text="Invented helper is unsafe",
                kind="finding",
                severity="critical",
                evidence="unsafe_helper(name)",
            ),
        ),
        "flash": PanelAnswer(summary="nothing in my area", none_found=True),
    }


def test_union_merges_duplicates_keeps_backed_findings_and_drops_invented_ones() -> None:
    lenses = {"haiku": "correctness", "luna": "security", "flash": "performance"}
    result = merge_findings(_review_answers(), _material(), lenses=lenses)
    assert [f.claim.text for f in result.backed] == [
        "String formatted SQL query allows injection",  # the quoted one leads the cluster
        "Missing check for an empty name",
    ]
    injection = result.backed[0]
    assert injection.claim.severity == "critical" and injection.models == ["haiku", "luna"]
    assert injection.lenses == ["correctness", "security"]
    assert [f.claim.text for f in result.dropped] == ["Invented helper is unsafe"]
    assert [f.claim.kind for f in result.advice] == ["test"]
    assert result.none_found == ["flash"]
    kept = union_claims(_review_answers(), _material(), lenses=lenses)
    assert [c.text for c in kept] == [f.claim.text for f in result.kept]
    assert "Invented helper is unsafe" not in [c.text for c in kept]


def test_union_ranks_by_severity() -> None:
    answers = {
        "a": _answer(
            _finding("Connection opened per call", evidence="conn = connect()", severity="low")
        ),
        "b": _answer(
            _finding("Injection", evidence="rows = conn.execute(query)", severity="critical")
        ),
        "c": _answer(
            _finding("All rows loaded", evidence="return rows.fetchall()", severity="med")
        ),
    }
    result = merge_findings(answers, _material())
    assert [f.claim.severity for f in result.backed] == ["critical", "med", "low"]


def test_union_renders_what_it_kept_what_it_left_out_and_who_found_nothing() -> None:
    lenses = {"haiku": "correctness", "luna": "security", "flash": "performance"}
    text = render_union(merge_findings(_review_answers(), _material(), lenses=lenses))
    assert text.startswith("## Panel union: 2 findings backed by the input, from 3 lenses")
    assert "Nothing to report from: performance (flash)." in text
    assert "### Findings backed by the input" in text
    assert "### Suggested changes and tests" in text
    assert "Left out 1 point(s) the input does not back: Invented helper is unsafe" in text


def test_union_without_material_checks_nothing_and_drops_nothing() -> None:
    result = merge_findings(_review_answers())
    assert not result.backed and not result.dropped
    assert result.unchecked  # every finding is kept, marked unchecked


def test_a_checker_confirmation_counts_only_with_a_quote_from_the_input() -> None:
    answers = {
        "a": _answer(_finding("Empty names are not rejected")),
        "b": _answer(_finding("Rows are never closed")),
        "c": _answer(_finding("Connection reused across threads")),
    }
    result = merge_findings(answers, _material())
    ids = {f.claim.text: f.cluster.id for f in result.unchecked}
    changed = apply_check(
        result,
        {
            ids["Empty names are not rejected"]: ("confirmed", "def find_user(name):"),
            ids["Rows are never closed"]: ("confirmed", "rows.close() missing"),  # not in input
            ids["Connection reused across threads"]: ("rejected", None),
        },
        _material(),
    )
    assert changed == 2
    assert [f.claim.text for f in result.backed] == ["Empty names are not rejected"]
    assert result.backed[0].claim.evidence == "def find_user(name):"
    assert [f.claim.text for f in result.unchecked] == ["Rows are never closed"]
    assert [f.claim.text for f in result.dropped] == ["Connection reused across threads"]


# ------------------------------------------------------------------------------ strategies


def _strategy(**kw: Any) -> Strategy:
    base: dict[str, Any] = {
        "name": "s",
        "kind": "panel",
        "members": [{"model": "claude-haiku"}, {"model": "gpt-luna"}],
    }
    return Strategy.model_validate({**base, **kw})


def test_lenses_default_to_auto_for_panels_that_keep_every_piece() -> None:
    assert _strategy().lenses == "auto"  # llm
    assert _strategy(aggregator="digest").lenses == "auto"
    assert _strategy(aggregator="union").lenses == "auto"
    assert _strategy(aggregator="vote").lenses == "off"
    assert _strategy(aggregator="best_of").lenses == "off"
    assert _strategy(lenses=False).lenses == "off"  # YAML `off`
    solo = Strategy.model_validate(
        {"name": "x", "kind": "solo", "members": [{"model": "claude-haiku"}]}
    )
    assert solo.lenses == "off"


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"aggregator": "vote", "lenses": "auto"}, "vote aggregator keeps only"),
        ({"aggregator": "verified", "lenses": "auto"}, "verified aggregator keeps only"),
        (
            {
                "kind": "cascade",
                "lenses": "auto",
                "members": [{"model": "claude-haiku"}, {"model": "gpt-luna"}, {"model": "x"}],
            },
            "a cascade stops",
        ),
        (
            {"members": [{"model": "gpt-luna"}, {"model": "gpt-luna"}], "lenses": "off"},
            "only with lenses",
        ),
        (
            {
                "members": [
                    {"model": "gpt-luna", "lens": "security"},
                    {"model": "gpt-luna", "lens": "security"},
                ]
            },
            "same lens",
        ),
        (
            {"aggregator": "union", "aggregator_model": "claude-haiku"},
            "aggregator_model must be unset",
        ),
    ],
)
def test_strategies_that_misuse_lenses_are_rejected(spec: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _strategy(**spec)


def test_a_member_naming_an_unknown_lens_is_rejected_at_load() -> None:
    from fusion.orchestration import strategy as strategy_module

    book = load_strategy_book()
    nano = book.get("panel-nano")
    broken = nano.model_copy(
        update={"members": [PanelMember(model="claude-haiku", lens="vibes"), *nano.members[1:]]}
    )
    bad = strategy_module.StrategyBook({"panel-nano": broken}, {}, None, book.lenses)
    with pytest.raises(ConfigError, match="member claude-haiku: Unknown lens 'vibes'"):
        strategy_module._check_lenses(bad)


def test_union_check_plans_one_cheap_call_and_union_none() -> None:
    from fusion.orchestration.budget_guard import plan_calls

    union = _strategy(aggregator="union")
    checked = _strategy(aggregator="union+check", aggregator_model="claude-haiku")
    assert [c.stage for c in plan_calls(union, judge_model="j", prompt_tokens=100)] == [
        "panel",
        "panel",
    ]
    stages = [c.stage for c in plan_calls(checked, judge_model="j", prompt_tokens=100)]
    assert stages == ["panel", "panel", "synthesis"]
    check = plan_calls(checked, judge_model="j", prompt_tokens=100)[-1]
    assert check.output_tokens == 400
    assert checked.calls_aggregator_model and not union.calls_aggregator_model


def test_a_lensed_plan_prices_the_lens_instructions() -> None:
    from fusion.orchestration.budget_guard import plan_calls
    from fusion.routing.budget import LENS_PROMPT_TOKENS, estimate_tokens

    lensed = plan_calls(_strategy(), judge_model="j", prompt_tokens=100)[0]
    plain = plan_calls(_strategy(lenses="off"), judge_model="j", prompt_tokens=100)[0]
    assert lensed.input_tokens - plain.input_tokens == LENS_PROMPT_TOKENS
    # the constant stays above the longest lens prompt the packaged lenses produce
    book = _book()
    longest = 0
    for task in (TaskType.CODE_REVIEW, TaskType.DEBUGGING, TaskType.IMPLEMENTATION_PLAN):
        for size in range(2, 6):
            given = assign_lenses(task, _members(*[f"m{i}" for i in range(size)]), book)
            for lens in given.values():
                longest = max(
                    longest, estimate_tokens(lens_instruction(lens, list(given.values())))
                )
    assert longest <= LENS_PROMPT_TOKENS


# ------------------------------------------------------------------------- end to end (mock)


def _pipe(tmp_path: Path, provider: MockProvider | None = None) -> Any:
    settings = Settings(use_mock=True, db_path=str(tmp_path / "l.db"))
    return build_pipeline(settings, {"mock": provider or MockProvider(latency_ms=0)})


def _review(**kw: Any) -> PipelineContext:
    return PipelineContext(
        task_type=TaskType.CODE_REVIEW, primary_content=DIFF, changed_files=["app/db.py"], **kw
    )


async def test_the_default_strategy_reviews_with_lenses(tmp_path: Path) -> None:
    result = await _pipe(tmp_path).run(_review())
    lenses = result.disagreement["lenses"]
    assert len(lenses) == len(result.panel_results) >= 2
    assert len(set(lenses.values())) == len(lenses)  # every member covers its own area
    assert any(r.startswith("Lenses: ") for r in result.routing.reasons)


async def test_a_union_run_returns_the_merged_findings_with_no_merge_call(tmp_path: Path) -> None:
    result = await _pipe(tmp_path).run(_review(strategy="panel-nano-union"))
    assert result.final_answer.startswith("## Panel union:")
    assert {r.stage for r in result.ledger.records} == {"panel"}  # no synthesis call
    union = result.disagreement["union"]
    answered = {p.model_name for p in result.panel_results}
    assert union["backed"] >= 1 and set(union["lenses"]) == answered
    # the structured fields come from what the union kept, not from every cluster
    kept = union["backed"] + union["unchecked"]
    assert len(result.structured_output["critical_findings"]) <= kept


async def test_a_self_mixture_asks_one_model_once_per_lens(tmp_path: Path) -> None:
    provider = MockProvider(latency_ms=0)
    pipe = _pipe(tmp_path, provider)
    result = await pipe.run(_review(strategy="panel-luna-lenses"))
    names = [p.model_name for p in result.panel_results]
    assert names == ["mock-fast", "mock-fast#2", "mock-fast#3"]
    assert sorted(result.disagreement["lenses"].values()) == [
        "correctness+contract",
        "performance",
        "security",
    ]
    calls = [r for r in result.ledger.records if r.stage == "panel"]
    assert {r.model_id for r in calls} == {"mock-fast"} and len(calls) == 3
    # the seats are priced as the model they copy
    assert all(r.cost_known for r in calls)
    # a task type with no lenses asks the model once
    ask = await pipe.run(
        PipelineContext(
            task_type=TaskType.DEFAULT,
            primary_content="How should I structure retries around a flaky HTTP call?",
            strategy="panel-luna-lenses",
        )
    )
    assert [p.model_name for p in ask.panel_results] == ["mock-fast"]
    assert any("runs 1 time(s), not 3" in r for r in ask.routing.reasons)


async def test_a_lensed_panel_waits_for_every_lens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__FANOUT__EARLY_RETURN__QUORUM", "2")
    monkeypatch.setenv("FUSION__FANOUT__EARLY_RETURN__GRACE_MS", "0")

    class Slow(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.model_id == "mock-weak" and request.metadata.get("role") == "panel":
                await asyncio.sleep(0.2)
            return await super().complete(request)

    result = await _pipe(tmp_path, Slow(latency_ms=0)).run(_review(strategy="panel-nano"))
    assert result.fanout is not None and not result.fanout.early_return
    assert {p.model_name for p in result.panel_results} == {
        "mock-fast",
        "mock-security",
        "mock-weak",
    }


async def test_a_lensed_refinement_round_is_not_skipped_for_agreement(tmp_path: Path) -> None:
    result = await _pipe(tmp_path).run(_review(strategy="panel-refine"))
    assert any(r.stage == "refine" for r in result.ledger.records)
    assert not any("Refinement skipped" in w for w in result.warnings)


async def test_a_lensed_vote_skip_builds_a_union(tmp_path: Path) -> None:
    """panel-duo skips synthesis when the panel agrees; on a lensed run the vote that would build
    the answer keeps only shared points, so the union does it instead."""

    class Same(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("role") == "panel":
                claim = {
                    "text": "String formatted SQL query allows injection",
                    "kind": "finding",
                    "severity": "high",
                    "file": "app/db.py",
                    "line": 20,
                    "evidence": 'query = f"SELECT',
                }
                own = {"text": f"Own point of {request.metadata.get('lens')}", "kind": "finding"}
                body = json.dumps({"summary": "s", "claims": [claim, own], "none_found": False})
                return ModelResponse(provider="mock", model=request.model_id, text=body)
            return await super().complete(request)

    result = await _pipe(tmp_path, Same(latency_ms=0)).run(_review(strategy="panel-duo"))
    assert result.final_answer.startswith("## Panel union:")
    assert "union built the answer" in " ".join(result.routing.reasons)
    assert "Own point of" in result.final_answer  # a lens-only point survived


async def test_union_check_settles_unquoted_findings_with_one_call(tmp_path: Path) -> None:
    class Checker(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            role = request.metadata.get("role")
            if role == "panel":
                claims = [{"text": "Empty names are not rejected", "kind": "finding"}]
                body = json.dumps({"summary": "s", "claims": claims})
                return ModelResponse(provider="mock", model=request.model_id, text=body)
            if role == "union_check":
                ids = [
                    line.split("]")[0].split("[")[-1]
                    for line in request.user_prompt.splitlines()
                    if line.startswith("- [")
                ]
                quote = "def find_user(name):"
                verdicts = [{"id": i, "verdict": "confirmed", "quote": quote} for i in ids]
                body = json.dumps({"verdicts": verdicts})
                return ModelResponse(provider="mock", model=request.model_id, text=body)
            return await super().complete(request)

    from fusion.orchestration.strategy import StrategyBook

    pipe = _pipe(tmp_path, Checker(latency_ms=0))
    book = pipe.deps.routing.strategies
    nano = book.get("panel-nano")  # the nano panel with union+check instead of synthesis
    checked = nano.model_copy(update={"name": "nano-check", "aggregator": "union+check"})
    pipe.deps.routing._strategies = StrategyBook(
        {**book.strategies, "nano-check": checked}, book.budget_map, book.aliases, book.lenses
    )
    result = await pipe.run(_review(strategy="nano-check"))
    stages = [r.stage for r in result.ledger.records]
    assert stages.count("synthesis") == 1  # the one check call
    assert result.disagreement["union"]["backed"] == 1
    assert result.disagreement["union"]["checked_by"]
    assert "checked the unquoted ones" in result.final_answer


async def test_a_fallback_for_a_lensed_member_keeps_its_lens() -> None:
    seen: list[ModelRequest] = []

    class Failing(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            seen.append(request)
            if request.model_id == "a":
                raise ServerError("down")
            return await super().complete(request)

    def entry(model_id: str, roles: list[str]) -> ModelEntry:
        return ModelEntry(
            alias=model_id,
            provider="mock",
            model_id=model_id,
            roles=roles,
            prices=[{"input_per_1m": 0.1, "output_per_1m": 0.5}],  # type: ignore[list-item]
        )

    models = {m: entry(m, ["panel"]) for m in ("a", "b", "spare")}
    providers = {"mock": Failing(latency_ms=0)}
    lenses = assign_lenses(TaskType.DEBUGGING, _members("a", "b"), _book())
    gateway = CallGateway(
        ledger=RunLedger(), models=models, providers=providers, pricing=PricingRegistry()
    )
    result = await fanout_to_panel(
        panel_models=["a", "b"],
        registry_models=models,
        providers=providers,
        task_type=TaskType.DEBUGGING,
        primary_content="TimeoutError in acquire()",
        config=FanoutConfig(),
        gateway=gateway,
        lenses=lenses,
    )
    assert result.fallbacks == {"a": "spare"}
    assert result.answered_for == {"spare": "a", "b": "b"}
    spare = next(r for r in seen if r.model_id == "spare")
    assert spare.metadata["lens"] == lenses["a"].name


def test_mock_book_keeps_a_self_mixture_on_one_mock_model() -> None:
    from fusion.orchestration.strategy import mock_strategy_book

    registry = ModelRegistry.for_mode(use_mock=True)
    book = mock_strategy_book(load_strategy_book(), registry)
    assert len({m.model for m in book.get("panel-luna-lenses").members}) == 1
    assert len({m.model for m in book.get("panel-nano").members}) == 3


def test_a_seat_is_a_roleless_copy_priced_as_its_model() -> None:
    registry = ModelRegistry.for_mode(use_mock=False)
    registry.add_seat("gpt-luna#2", "gpt-luna")
    seat, base = registry.get("gpt-luna#2"), registry.get("gpt-luna")
    assert seat.model_id == base.model_id and seat.roles == [] and seat.alias == "gpt-luna"
    assert "gpt-luna#2" not in registry.by_role("panel")
    assert seat.prices == base.prices
