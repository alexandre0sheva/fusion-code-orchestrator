"""Claims, deterministic clustering, agreement and calibrated confidence."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from fusion.evals.final_eval import evaluate_final_answer
from fusion.evals.structural import structural_scores
from fusion.mcp_server.schemas import FusionAskInput as McpAskInput
from fusion.mcp_server.tools import FusionTools
from fusion.orchestration.claims import (
    LINE_WINDOW,
    AgreementReport,
    Claim,
    PanelAnswer,
    agreement_score,
    calibrated_confidence,
    cluster_claims,
    panel_answer_schema,
    parse_panel_answer,
    render_panel_answer,
)
from fusion.orchestration.context import PipelineContext
from fusion.orchestration.factory import Settings, build_pipeline
from fusion.orchestration.schemas import FusionAskInput
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.providers.mock import MockProvider
from fusion.routing.classifier import TaskType

LONG = "Retries around a flaky HTTP call keep timing out in production under load."


def claim(text: str, kind: str = "finding", **kw: Any) -> Claim:
    return Claim(text=text, kind=kind, **kw)  # type: ignore[arg-type]


def answer(*claims: Claim, **kw: Any) -> PanelAnswer:
    return PanelAnswer(summary="s", claims=list(claims), **kw)


def sizes(answers: dict[str, PanelAnswer]) -> list[tuple[str, ...]]:
    return sorted(tuple(c.models) for c in cluster_claims(answers))


# ---------------------------------------------------------------------------------- the schema


def test_panel_answer_schema_is_strict_and_self_contained() -> None:
    schema = panel_answer_schema()
    assert "$ref" not in json.dumps(schema) and "$defs" not in schema

    def objects(node: Any) -> list[dict[str, Any]]:
        if isinstance(node, list):
            return [o for item in node for o in objects(item)]
        if not isinstance(node, dict):
            return []
        found = [node] if node.get("type") == "object" else []
        return found + [o for v in node.values() for o in objects(v)]

    nested = objects(schema)
    assert len(nested) == 2  # the answer and each claim
    for obj in nested:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])
    claim_props = schema["properties"]["claims"]["items"]["properties"]
    assert claim_props["kind"]["enum"] == [
        "finding",
        "hypothesis",
        "recommendation",
        "risk",
        "test",
    ]
    assert "default" not in json.dumps(schema)


# --------------------------------------------------------------------------------------- parsing


def test_valid_claims_json_parses_and_numbers_the_claims() -> None:
    text = json.dumps(
        {
            "summary": "ok",
            "claims": [
                {"text": "A", "kind": "finding", "severity": "high", "file": "a.py", "line": 4},
                {"text": "B", "kind": "test"},
            ],
            "confidence": 0.8,
        }
    )
    parsed, valid = parse_panel_answer(text)
    assert valid and [c.id for c in parsed.claims] == ["c1", "c2"]
    assert parsed.claims[0].file == "a.py" and parsed.claims[0].line == 4
    assert parsed.confidence == 0.8


def test_parser_tolerates_common_model_slips() -> None:
    data = {
        "summary": "x",
        "claims": [
            {"text": "bug here", "kind": "bug", "severity": "medium", "line": True},
            {"text": "do this", "kind": "fix", "severity": "urgent", "line": -3},
            {"text": "  ", "kind": "finding"},
            "not a claim",
            {"text": "odd", "kind": "poem"},
        ],
        "confidence": 7,
    }
    parsed, valid = parse_panel_answer("", data, task_key="debugging")
    assert valid
    first, second, third = parsed.claims
    assert (first.kind, first.severity, first.line) == ("finding", "med", None)
    assert (second.kind, second.severity, second.line) == ("recommendation", None, None)
    assert third.kind == "hypothesis"  # unknown kinds take the task's default kind
    assert parsed.confidence == 1.0  # clamped


def test_prose_is_read_from_list_items_and_flagged_unstructured() -> None:
    prose = "Review\n1. Null check missing at app/db.py:12\n- Add a test\nnot an item"
    parsed, valid = parse_panel_answer(prose, task_key="code_review")
    assert not valid
    assert [c.text for c in parsed.claims] == ["Null check missing at app/db.py:12", "Add a test"]
    assert (parsed.claims[0].file, parsed.claims[0].line) == ("app/db.py", 12)
    assert all(c.kind == "finding" for c in parsed.claims)


def test_render_panel_answer_shows_severity_kind_and_location() -> None:
    text = render_panel_answer(
        answer(claim("Leak", severity="high", file="a.py", line=2), claim("Add test", "test"))
    )
    assert text.splitlines() == ["s", "", "- [high] finding: Leak (a.py:2)", "- test: Add test"]


# ------------------------------------------------------------------------------------ clustering


def test_same_file_within_three_lines_merges_even_with_different_words() -> None:
    answers = {
        "a": answer(claim("Unchecked cursor result", file="db.py", line=10)),
        "b": answer(claim("Null deref when the row is empty", file="db.py", line=10 + LINE_WINDOW)),
    }
    assert sizes(answers) == [("a", "b")]


def test_same_file_four_lines_apart_with_unrelated_words_does_not_merge() -> None:
    answers = {
        "a": answer(claim("Unchecked cursor result", file="db.py", line=10)),
        "b": answer(claim("Null deref when the row is empty", file="db.py", line=14)),
    }
    assert sizes(answers) == [("a",), ("b",)]


def test_different_files_do_not_merge_by_line_alone() -> None:
    answers = {
        "a": answer(claim("Unchecked cursor result", file="db.py", line=10)),
        "b": answer(claim("Null deref when the row is empty", file="api.py", line=10)),
    }
    assert sizes(answers) == [("a",), ("b",)]


def test_different_category_never_merges_even_when_identical() -> None:
    same = dict(file="db.py", line=10)
    answers = {
        "a": answer(claim("Add retries around the call", "finding", **same)),
        "b": answer(claim("Add retries around the call", "recommendation", **same)),
    }
    assert sizes(answers) == [("a",), ("b",)]


def test_similar_wording_merges_without_any_location() -> None:
    answers = {
        "a": answer(claim("Missing error handling around the database call")),
        "b": answer(claim("The database call is missing error handling")),
        "c": answer(claim("Variable names are inconsistent")),
    }
    assert sizes(answers) == [("a", "b"), ("c",)]


def test_a_cluster_holds_one_claim_per_model() -> None:
    answers = {
        "a": answer(
            claim("Missing error handling around the database call"),
            claim("Missing error handling around the database call in the loop"),
        ),
        "b": answer(claim("Missing error handling around the database call")),
    }
    clusters = cluster_claims(answers)
    assert sorted(len(c.models) for c in clusters) == [1, 2]
    assert all(len({m.model for m in c.members}) == len(c.members) for c in clusters)


def test_clustering_does_not_depend_on_the_order_models_arrive_in() -> None:
    a = answer(claim("Unchecked cursor result", file="db.py", line=10), claim("Add a test", "test"))
    b = answer(claim("Cursor result is unchecked", file="db.py", line=11))
    c = answer(claim("Add a test", "test"), claim("Something else entirely"))
    forward = cluster_claims({"a": a, "b": b, "c": c})
    backward = cluster_claims({"c": c, "b": b, "a": a})
    assert [(x.id, x.text, x.models) for x in forward] == [
        (x.id, x.text, x.models) for x in backward
    ]


def test_the_representative_is_the_most_severe_claim() -> None:
    answers = {
        "a": answer(claim("Query built from a string", severity="med", file="d.py", line=5)),
        "b": answer(claim("SQL injection", severity="critical", file="d.py", line=6)),
    }
    (cluster,) = cluster_claims(answers)
    assert (cluster.text, cluster.severity, cluster.status) == (
        "SQL injection",
        "critical",
        "unique",  # status is filled in by agreement_score
    )


# ------------------------------------------------------------------------------------ agreement


def _report(answers: dict[str, PanelAnswer], **kw: Any) -> AgreementReport:
    return agreement_score(cluster_claims(answers), len(answers), **kw)


def test_identical_answers_agree_completely_and_disjoint_ones_not_at_all() -> None:
    same = {m: answer(claim("Missing error handling"), claim("Add a test", "test")) for m in "abc"}
    assert _report(same).score == pytest.approx(1.0)
    disjoint = {
        "a": answer(claim("alpha beta gamma")),
        "b": answer(claim("delta epsilon zeta")),
        "c": answer(claim("eta theta iota")),
    }
    assert _report(disjoint).score == 0.0


def test_agreement_rises_as_more_claims_are_shared() -> None:
    def build(shared: int) -> dict[str, PanelAnswer]:
        topics = ["alpha beta", "gamma delta", "epsilon zeta", "eta theta"]
        out = {}
        for i, model in enumerate("abc"):
            claims = [claim(t) for t in topics[:shared]]
            claims.append(claim(f"private{i} point{i}"))
            out[model] = answer(*claims)
        return out

    scores = [_report(build(n)).score for n in range(5)]
    assert (
        scores == sorted(scores) and len(set(scores)) == 5 and scores[0] == 0.0 and scores[-1] > 0.5
    )


def test_consensus_unique_and_contradicted_sets_are_disjoint_and_complete() -> None:
    answers = {
        "a": answer(
            claim("Shared point", severity="med"),
            claim("Disputed point", severity="low"),
            claim("Only a"),
        ),
        "b": answer(
            claim("Shared point", severity="med"), claim("Disputed point", severity="high")
        ),
        "c": answer(claim("Elsewhere unrelated words")),
    }
    clusters = cluster_claims(answers)
    report = agreement_score(clusters, 3)
    ids = report.consensus + report.unique + report.contradicted
    assert len(ids) == len(set(ids))
    assert set(ids) == {c.id for c in clusters}
    by_text = {c.text: c for c in clusters}
    assert by_text["Disputed point"].id in report.contradicted
    assert by_text["Shared point"].id in report.consensus
    assert by_text["Only a"].id in report.unique


def test_a_severity_dispute_lowers_agreement() -> None:
    def pair(second: str) -> float:
        return _report(
            {
                "a": answer(claim("Same issue here", severity="low")),
                "b": answer(claim("Same issue here", severity=second)),  # type: ignore[arg-type]
            }
        ).score

    assert pair("med") == 1.0 and pair("critical") == 0.5


def test_majority_needs_at_least_two_and_half_the_panel() -> None:
    four = {m: answer(claim("x y z") if m in "ab" else claim(f"{m} {m}{m}")) for m in "abcd"}
    assert len(_report(four).consensus) == 1  # 2 of 4
    five = {m: answer(claim("x y z") if m in "ab" else claim(f"{m} {m}{m}")) for m in "abcde"}
    report = _report(five)
    assert report.consensus == [] and "C1" not in report.unique  # 2 of 5 is only partial


def test_one_model_has_nobody_to_agree_with() -> None:
    report = _report({"a": answer(claim("Anything at all", file="a.py", line=1, evidence="e"))})
    assert report.low_information and report.score == 0.0 and report.outliers == []


def test_outliers_need_three_models_and_some_agreement_among_the_others() -> None:
    shared = claim("Missing error handling")
    three = {
        "a": answer(shared),
        "b": answer(shared),
        "c": answer(claim("completely different thing")),
    }
    assert _report(three).outliers == ["c"]
    two = {"a": answer(shared), "b": answer(claim("completely different thing"))}
    assert _report(two).outliers == []


# ------------------------------------------------------------------------------------ confidence


def test_confidence_never_exceeds_half_for_one_model_with_or_without_evidence() -> None:
    bare = _report({"a": answer(claim("Anything at all"))})
    assert bare.low_information and bare.confidence <= 0.5
    backed = _report({"a": answer(claim("Anything at all", file="a.py", line=1, evidence="quote"))})
    assert backed.confidence <= 0.5
    assert bare.confidence < backed.confidence  # evidence still counts
    assert bare.confidence == pytest.approx(0.2)  # 0.2 * coverage, nothing else to go on


def test_confidence_is_the_documented_formula() -> None:
    report = AgreementReport(
        n_models=3,
        n_requested=4,
        n_clusters=5,
        score=0.6,
        contradicted=["C1"],
        evidence_rate=0.5,
        coverage=0.75,
        structured_share=2 / 3,
        low_information=False,
    )
    expected = 0.5 * 0.6 + 0.3 * 0.5 + 0.2 * (0.75 * 2 / 3) - 0.15 * 1 / 5
    assert calibrated_confidence(report) == pytest.approx(round(expected, 4))


def test_confidence_rises_with_agreement_evidence_and_coverage() -> None:
    base = dict(n_models=3, n_requested=3, n_clusters=4, low_information=False)

    def conf(**kw: Any) -> float:
        fields = {
            **base,
            "score": 0.3,
            "evidence_rate": 0.3,
            "coverage": 1.0,
            "structured_share": 1.0,
        }
        return calibrated_confidence(AgreementReport(**{**fields, **kw}))

    assert conf(score=0.8) > conf(score=0.3)
    assert conf(evidence_rate=0.9) > conf(evidence_rate=0.3)
    assert conf(coverage=1.0) > conf(coverage=0.5)
    assert conf(structured_share=1.0) > conf(structured_share=0.0)
    assert conf(contradicted=["C1"]) < conf()


def test_no_answers_means_no_confidence() -> None:
    assert agreement_score([], 0).confidence == 0.0


def test_confidence_does_not_depend_on_how_the_answer_is_worded() -> None:
    answers = {
        m: answer(claim("Shared point here", file="a.py", line=3, evidence="q")) for m in "ab"
    }
    clusters = cluster_claims(answers)
    report = agreement_score(clusters, 2)
    confident = "I strongly recommend this fix; confidence is very high. Add a test. Risk: none."
    hedged = "Maybe? Unsure. Could be anything."
    one = evaluate_final_answer(confident, is_coding_task=True, clusters=clusters, report=report)
    two = evaluate_final_answer(hedged, is_coding_task=True, clusters=clusters, report=report)
    assert one.model_dump(exclude={"notes"}) == two.model_dump(exclude={"notes"})
    assert one.confidence == report.confidence


def test_a_final_answer_without_claims_scores_zero_not_a_guess() -> None:
    result = evaluate_final_answer("Fusion panel quorum was not met.", clusters=None, report=None)
    assert result.confidence == 0.0 and result.overall_score == 0.0
    assert "No panel claims" in result.notes


def test_final_eval_measures_tests_residual_risk_and_readiness_from_claims() -> None:
    answers = {
        "a": answer(
            claim("SQL injection", severity="critical", file="d.py", line=4),
            claim("Add a regression test", "test"),
        )
    }
    clusters = cluster_claims(answers)
    report = agreement_score(clusters, 1)
    coding = evaluate_final_answer("x", is_coding_task=True, clusters=clusters, report=report)
    assert coding.test_plan_quality == 1.0 and coding.residual_risk == 0.9
    assert coding.implementation_readiness == 0.5 and coding.claude_code_usefulness == 0.5
    no_test = cluster_claims({"a": answer(claim("SQL injection", severity="critical"))})
    bare = evaluate_final_answer(
        "x", is_coding_task=True, clusters=no_test, report=agreement_score(no_test, 1)
    )
    assert bare.test_plan_quality == 0.0
    assert "No test claim in a coding answer" in bare.deterministic_issues
    assert not bare.deterministic_passed


# ------------------------------------------------------------------------- structural scoring


def test_structural_scores_are_shares_of_measurable_claim_properties() -> None:
    scores = structural_scores(
        answer(
            claim("A", file="a.py", line=1, evidence="q", severity="low"),
            claim("B", "recommendation"),
            claim("C", "test", file="zzz.py"),
            claim("D"),
        ),
        structured=True,
        known_files=["a.py"],
    )
    assert scores["specificity"] == 0.5 and scores["groundedness"] == 0.25
    assert scores["actionability"] == 0.5 and scores["codebase_awareness"] == 0.5
    assert scores["unsupported_claims"] == 0.25  # zzz.py is not among the provided files
    assert scores["correctness_likelihood"] == 0.5  # cannot be measured without a judge


def test_unstructured_answers_get_neutral_scores_whatever_the_text() -> None:
    scores = structural_scores(None, structured=False)
    dims = ("specificity", "groundedness", "actionability", "risk_awareness", "novelty")
    assert all(scores[d] == 0.5 for d in dims)
    assert scores["unsupported_claims"] == 0.0 and "not valid claims JSON" in scores["notes"]


def test_no_keyword_scoring_remains_in_evals() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "fusion"
    banned = re.compile(r"""(["'])(risk|recommend|should|caveat|uncertain|assumption)\1\s+in\s""")
    offenders = []
    for name in (
        "evals/final_eval.py",
        "evals/llm_judge.py",
        "evals/structural.py",
        "evals/answer_eval.py",
        "evals/deterministic.py",
        "orchestration/disagreement.py",
        "orchestration/output_parser.py",
        "orchestration/claims.py",
    ):
        text = (root / name).read_text()
        offenders += [name for _ in banned.finditer(text)]
        assert "heuristic_judge_scores" not in text
        assert "check_includes_uncertainty" not in text
    assert offenders == []
    assert not (root / "evals" / "disagreement_eval.py").exists()


# ----------------------------------------------------------------- panelists get the real schema


class Spy(MockProvider):
    def __init__(self) -> None:
        super().__init__(latency_ms=0.0)
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


def _pipe(tmp_path: Path, provider: MockProvider | None = None, **kw: Any) -> Any:
    return build_pipeline(
        Settings(use_mock=True, db_path=str(tmp_path / "c.db"), **kw),
        {"mock": provider or MockProvider(latency_ms=0.0)},
    )


def _ctx(**kw: Any) -> PipelineContext:
    return PipelineContext(
        task_type=TaskType.CODE_REVIEW,
        primary_content="diff --git a/app/db.py b/app/db.py\n+cursor.execute(q)",
        changed_files=["app/db.py"],
        **kw,
    )


async def test_panel_and_refine_requests_carry_the_claims_schema(tmp_path: Path) -> None:
    spy = Spy()
    await _pipe(tmp_path, spy).run(_ctx(strategy="panel-refine"))
    panel = [r for r in spy.requests if r.metadata.get("role") in {"panel", "refine"}]
    assert len(panel) == 6
    schema = panel_answer_schema()
    for request in panel:
        assert request.response_schema == schema
        assert request.response_schema_name == "panel_answer"
    first = next(r for r in panel if r.metadata["role"] == "panel")
    assert "ONE atomic" in first.user_prompt and "`claims`" in first.user_prompt


async def test_the_synthesizer_sees_clusters_and_readable_answers_not_raw_json(
    tmp_path: Path,
) -> None:
    spy = Spy()
    await _pipe(tmp_path, spy).run(_ctx())
    synthesis = next(r for r in spy.requests if r.metadata.get("role") == "synthesizer")
    prompt = synthesis.user_prompt
    assert "## Claim clusters" in prompt
    assert re.search(r"\[C1\] consensus, 2 model\(s\) \(mock-fast, mock-security\)", prompt)
    assert "Agreement summary" in prompt
    assert '"claims"' not in prompt.split("## Response from")[1]  # answers are rendered Markdown


async def test_the_shadow_baseline_gets_a_free_form_prompt(tmp_path: Path) -> None:
    spy = Spy()
    await _pipe(tmp_path, spy).run(_ctx(shadow_baseline=True))
    baseline = [r for r in spy.requests if r.metadata.get("role") == "shadow_baseline"]
    assert baseline and all("`claims`" not in r.user_prompt for r in baseline)
    assert all("Respond with structured analysis" in r.user_prompt for r in baseline)


async def test_confidence_comes_from_agreement_not_from_the_synthesizer(tmp_path: Path) -> None:
    result = await _pipe(tmp_path).run(_ctx())
    assert result.agreement is not None
    assert result.final_eval.confidence == result.agreement.confidence
    assert result.structured_output["confidence"] == result.agreement.confidence
    assert result.structured_output["confidence"] != 0.72  # the mock synthesizer's own claim


async def test_unstructured_panel_answers_are_flagged_and_lower_confidence(tmp_path: Path) -> None:
    class Prose(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("role") == "panel":
                return ModelResponse(
                    provider="mock",
                    model=request.model_id,
                    text=f"## Review of {request.model_id}\n1. Missing error handling in db.py:3",
                    input_tokens=5,
                    output_tokens=5,
                )
            return await super().complete(request)

    plain = await _pipe(tmp_path).run(_ctx())
    prose = await _pipe(tmp_path, Prose(latency_ms=0.0)).run(_ctx())
    assert any("not valid claims JSON" in w for w in prose.warnings)
    assert prose.agreement is not None and prose.agreement.structured_share == 0.0
    assert prose.claims and prose.claims[0].support == 3  # still clustered from list items
    assert plain.agreement is not None and plain.agreement.structured_share == 1.0


async def test_one_answer_is_flagged_low_information_in_the_output(tmp_path: Path) -> None:
    result = await _pipe(tmp_path).run(_ctx(strategy="solo-cheap"))
    assert result.agreement is not None and result.agreement.low_information
    assert result.final_eval.confidence <= 0.5
    assert any("low-information" in w for w in result.warnings)
    assert result.disagreement["low_information"] is True


async def test_cited_files_outside_the_provided_ones_are_reported(tmp_path: Path) -> None:
    ctx = PipelineContext(
        task_type=TaskType.CODE_REVIEW,
        primary_content=_ctx().primary_content,
        changed_files=["app/db.py"],
    )
    ok = await _pipe(tmp_path).run(ctx)
    assert ok.disagreement["unsupported_claims"] == []
    # The same answers judged against a different list of provided files.
    claims = ok.claims
    from fusion.orchestration.disagreement import analyze_disagreement

    assert ok.agreement is not None
    other = analyze_disagreement(claims, ok.agreement, known_files=["elsewhere.py"])
    assert any(
        "app/db.py, which is not among the provided files" in u for u in other["unsupported_claims"]
    )


# ---------------------------------------------------------- tool fields and display, per detail


async def test_every_tool_still_fills_its_fields_when_there_is_no_synthesizer(
    tmp_path: Path,
) -> None:
    tools = FusionTools(db_path=str(tmp_path / "t.db"), use_mock=True)
    from fusion.mcp_server import schemas as s

    review = await tools.fusion_review_diff(
        s.ReviewDiffInput(diff=LONG, changed_files=["a.py"], strategy="panel-digest")
    )
    assert review["critical_findings"] and review["recommended_changes"] and review["test_plan"]
    assert review["unique_insights"]
    debug = await tools.fusion_debug_error(
        s.DebugErrorInput(error_message=LONG, strategy="solo-cheap")
    )
    assert debug["ranked_hypotheses"] and {"hypothesis", "confidence", "evidence"} <= set(
        debug["ranked_hypotheses"][0]
    )
    assert debug["most_likely_causes"] and debug["verification_steps"]
    decide = await tools.fusion_decide_architecture(
        s.DecideArchitectureInput(question=LONG, strategy="solo-cheap")
    )
    assert decide["recommended_option"] and decide["test_strategy"]
    plan = await tools.fusion_plan_feature(
        s.PlanFeatureInput(feature_description=LONG, strategy="solo-cheap")
    )
    assert plan["implementation_sequence"] and plan["tests_to_add"]
    ask = await tools.fusion_ask(McpAskInput(prompt=LONG, strategy="solo-cheap"))
    assert ask["answer"] and ask["suggested_actions"]
    assert 0.0 <= ask["confidence"] <= 0.5
    evaluation = await tools.fusion_eval_answer(s.EvalAnswerInput(question=LONG, answer=LONG))
    assert evaluation["score"] > 0
    await tools.aclose()


async def test_compact_is_the_default_and_full_adds_everything(tmp_path: Path) -> None:
    assert FusionAskInput(prompt="x").detail == "compact"
    assert McpAskInput(prompt="x").detail == "compact"
    ask = build_pipeline_for(tmp_path)
    compact = await ask.ask(FusionAskInput(prompt="How should I retry an HTTP call in httpx?"))
    full = await ask.ask(
        FusionAskInput(prompt="How should I retry an HTTP call in httpx?", detail="full")
    )
    assert "### Key claims" in compact.display_markdown
    assert "### Cost & usage" not in compact.display_markdown
    assert compact.display_markdown.count("\n- ") < full.display_markdown.count("\n- ")
    assert "### Cost\n" in compact.display_markdown
    assert "### Cost & usage" in full.display_markdown and "### Claims" in full.display_markdown
    assert len(compact.display_markdown) < len(full.display_markdown)


def build_pipeline_for(tmp_path: Path) -> Any:
    from fusion.orchestration.factory import build_pipelines

    pipes = build_pipelines(
        Settings(use_mock=True, db_path=str(tmp_path / "p.db")),
        {"mock": MockProvider(latency_ms=0.0)},
    )
    return pipes["ask"]


async def test_display_text_is_never_cut_at_a_fixed_length(tmp_path: Path) -> None:
    long_summary = "Long answer sentence. " * 300  # 6,600 characters

    class Long(MockProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            if request.metadata.get("role") == "panel":
                body = json.dumps({"summary": long_summary, "claims": [], "confidence": 0.5})
                return ModelResponse(provider="mock", model=request.model_id, text=body)
            return await super().complete(request)

    ask = build_pipeline_for(tmp_path)
    ask.deps.providers["mock"] = Long(latency_ms=0.0)
    for detail in ("compact", "full"):
        out = await ask.ask(
            FusionAskInput(prompt="Explain it at length", strategy="solo-cheap", detail=detail)
        )
        assert long_summary.strip() in out.display_markdown
    source = (
        Path(__file__).resolve().parents[1] / "src/fusion/orchestration/output.py"
    ).read_text()
    assert "[:1200]" not in source
