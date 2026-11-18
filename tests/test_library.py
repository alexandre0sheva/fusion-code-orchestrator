"""The experience library (Task 12): per-project lessons distilled from runs the host gave feedback
on, retrieved into later panel prompts as hypotheses, stored outside the project and off by
default."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from _scripted import FAST, Scripted, answer
from fusion.config.loader import LibraryConfig, load_library_config
from fusion.library.distil import (
    Distilled,
    apply_distillation,
    evidence_line,
    overall_verdict,
    parse_distillation,
)
from fusion.library.retrieve import render_lessons, retrieve
from fusion.library.store import Feedback, Lesson, LibraryStore, inside, project_key
from fusion.mcp_server.schemas import FeedbackInput, FusionAskInput
from fusion.mcp_server.tools import FusionTools
from fusion.orchestration.factory import Settings, build_pipelines
from fusion.orchestration.prompts import compose_user_prompt
from fusion.providers.base import ModelRequest, ModelResponse
from fusion.routing.budget import estimate_tokens
from fusion.routing.classifier import TaskType

CUE = "a retry loop around an HTTP client call"
LESSON = "Check that only idempotent requests are retried before recommending backoff."


def lesson(**kw: Any) -> Lesson:
    fields: dict[str, Any] = {
        "project_key": "p1",
        "task_type": "default",
        "cue": CUE,
        "lesson": LESSON,
        "evidence": [evidence_line("helped", "run-1", "used", "")],
        "verdict": "helped",
        "confidence": "medium",
    }
    fields.update(kw)
    return Lesson.new(**fields)


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode())
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


# ------------------------------------------------------------------------------------ the store


def test_project_key_is_a_stable_hash_of_the_resolved_root(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    key = project_key(tmp_path / "a")
    assert key == project_key(tmp_path / "a" / ".." / "a")
    assert key != project_key(tmp_path / "b")
    assert len(key) == 16 and str(tmp_path) not in key


def test_lessons_round_trip_and_forget_deletes(tmp_path: Path) -> None:
    store = LibraryStore(db_path=tmp_path / "runs.db")
    first, other = lesson(), lesson(project_key="p2", cue="a schema migration")
    store.save(first)
    store.save(other)
    assert store.get(first.id) == first
    assert [x.id for x in store.lessons("p1")] == [first.id]
    assert {x.id for x in store.lessons()} == {first.id, other.id}
    assert store.forget(first.id) is True
    assert store.get(first.id) is None
    assert store.forget(first.id) is False
    assert store.forget_project("p2") == 1
    assert store.lessons() == []
    store.close()


def test_feedback_and_lesson_uses_round_trip(tmp_path: Path) -> None:
    store = LibraryStore(db_path=tmp_path / "runs.db")
    note = Feedback(run_id="r1", project_key="p1", task_type="default", outcome="wrong", note="n")
    store.record_feedback(note)
    assert store.feedback("r1") == note
    store.record_feedback(note.model_copy(update={"outcome": "used"}))  # the latest one wins
    got = store.feedback("r1")
    assert got is not None and got.outcome == "used"
    store.record_uses("r2", ["L1", "L2"])
    assert store.lessons_used("r2") == ["L1", "L2"]
    assert store.runs_using("L1") == ["r2"]
    store.close()


def test_the_library_refuses_a_database_inside_the_project(tmp_path: Path) -> None:
    assert inside(tmp_path / "proj" / ".fusion" / "runs.db", tmp_path / "proj")
    assert not inside(tmp_path / "data" / "runs.db", tmp_path / "proj")


def test_the_library_is_off_by_default() -> None:
    config = load_library_config()
    assert config.enabled is False
    assert config.max_prompt_tokens == 300 and config.max_lessons == 3


# ----------------------------------------------------------------------------- distillation


def distilled(**kw: Any) -> Distilled:
    fields: dict[str, Any] = {
        "cue": CUE,
        "lesson": LESSON,
        "verdict": "hurt",
        "confidence": "high",
    }
    fields.update(kw)
    return Distilled(**fields)


def test_no_entry_and_unparsable_replies_store_nothing() -> None:
    assert parse_distillation("NO_ENTRY") is None
    assert parse_distillation('{"entry": "NO_ENTRY"}') is None
    assert parse_distillation("I think the lesson is to be careful.") is None
    assert parse_distillation('{"cue": "x", "lesson": "y", "verdict": "great"}') is None
    got = parse_distillation(json.dumps(distilled().model_dump()))
    assert got == distilled()


def test_a_lesson_resting_only_on_confounded_evidence_is_not_stored() -> None:
    kept, action = apply_distillation(
        distilled(verdict="confounded"),
        existing=[],
        run_id="r1",
        outcome="wrong",
        note="",
        project_key="p1",
        task_type="default",
    )
    assert kept is None and action == "none"


def test_one_run_of_evidence_caps_confidence_at_medium() -> None:
    kept, action = apply_distillation(
        distilled(),
        existing=[],
        run_id="r1",
        outcome="wrong",
        note="missed the POST retry",
        project_key="p1",
        task_type="default",
    )
    assert action == "created" and kept is not None
    assert kept.confidence == "medium" and kept.verdict == "hurt"
    assert kept.evidence == [evidence_line("hurt", "r1", "wrong", "missed the POST retry")]


def test_refining_appends_evidence_and_agreement_raises_confidence() -> None:
    base = lesson()
    kept, action = apply_distillation(
        distilled(verdict="helped", refines=base.id),
        existing=[base],
        run_id="r2",
        outcome="used",
        note="",
        project_key="p1",
        task_type="default",
    )
    assert action == "updated" and kept is not None and kept.id == base.id
    assert len(kept.evidence) == 2 and kept.verdict == "helped" and kept.confidence == "high"


def test_conflicting_evidence_makes_the_verdict_confounded_and_confidence_low() -> None:
    base = lesson()
    kept, action = apply_distillation(
        distilled(verdict="hurt", refines=base.id, cue="a narrower cue"),
        existing=[base],
        run_id="r2",
        outcome="wrong",
        note="",
        project_key="p1",
        task_type="default",
    )
    assert action == "updated" and kept is not None
    assert kept.verdict == "confounded" and kept.confidence == "low"
    assert kept.cue == "a narrower cue"  # narrowed by the distiller


def test_a_lesson_left_with_only_confounded_evidence_is_dropped() -> None:
    base = lesson(evidence=[evidence_line("confounded", "r0", "partly", "")], verdict="helped")
    kept, action = apply_distillation(
        distilled(verdict="confounded", refines=base.id),
        existing=[base],
        run_id="r2",
        outcome="partly",
        note="",
        project_key="p1",
        task_type="default",
    )
    assert kept is not None and kept.id == base.id and action == "dropped"


def test_overall_verdict_reads_the_evidence_lines() -> None:
    assert overall_verdict([evidence_line("helped", "a", "used", "")]) == "helped"
    assert overall_verdict([evidence_line("confounded", "a", "used", "")]) is None
    mixed = [evidence_line("helped", "a", "used", ""), evidence_line("hurt", "b", "wrong", "")]
    assert overall_verdict(mixed) == "confounded"


# -------------------------------------------------------------------------------- retrieval


def test_retrieval_matches_project_task_type_and_words(tmp_path: Path) -> None:
    store = LibraryStore(db_path=tmp_path / "runs.db")
    match = lesson()
    store.save(match)
    store.save(lesson(project_key="other"))
    store.save(lesson(task_type="code_review"))
    store.save(lesson(cue="a CSS layout question", lesson="Prefer grid over floats."))
    found = retrieve("p1", "default", "How do I retry a failed HTTP call?", store=store)
    assert [x.id for x in found] == [match.id]
    assert retrieve("p1", "default", "Rename a variable", store=store) == []
    store.close()


def test_retrieval_returns_at_most_k(tmp_path: Path) -> None:
    store = LibraryStore(db_path=tmp_path / "runs.db")
    for _ in range(5):
        store.save(lesson())
    assert len(retrieve("p1", "default", "retry an HTTP call", k=3, store=store)) == 3
    store.close()


def test_rendered_lessons_are_hypotheses_and_bounded() -> None:
    long = [lesson(lesson="Check the retry policy. " * 24) for _ in range(3)]
    block = render_lessons(long, max_tokens=300)
    assert estimate_tokens(block.text) <= 300
    assert "hypotheses" in block.text.lower() and "re-check" in block.text.lower()
    assert block.ids and set(block.ids) <= {x.id for x in long}
    assert render_lessons([], max_tokens=300).text == ""


def test_lessons_go_after_the_files_and_before_the_request() -> None:
    kw: dict[str, Any] = {
        "task_type": TaskType.DEFAULT,
        "primary_content": "the request",
        "file_snippets": ["a file"],
    }
    plain = compose_user_prompt(**kw)
    with_lessons = compose_user_prompt(**kw, lessons="LESSON TEXT")
    assert with_lessons.shared_prefix == plain.shared_prefix  # the files' cache prefix holds
    text = with_lessons.text
    assert text.index("a file") < text.index("LESSON TEXT") < text.index("the request")
    assert "LESSON TEXT" not in plain.text


# ------------------------------------------------------------------- through the MCP tools


class Librarian(Scripted):
    """Scripted panel answers, plus a scripted reply for the distillation call."""

    def __init__(self, reply: str, answers: dict[str, str] | None = None) -> None:
        super().__init__(answers)
        self.reply = reply

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.metadata.get("role") == "librarian":
            self.requests.append(request)
            return ModelResponse(
                provider="mock",
                model=request.model_id,
                text=self.reply,
                input_tokens=100,
                output_tokens=40,
                cost_estimate_usd=0.0002,
            )
        return await super().complete(request)


LEARNED = json.dumps(
    {
        "cue": "retry logic around an HTTP client call",
        "lesson": "Say which HTTP methods are safe to retry before suggesting backoff.",
        "verdict": "hurt",
        "confidence": "medium",
        "refines": None,
    }
)
QUESTION = "How should I retry a failed HTTP call in httpx?"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.py").write_text("import httpx\n")
    return root


def tools_for(tmp_path: Path, provider: Scripted, db: Path | None = None) -> FusionTools:
    path = str(db or tmp_path / "s.db")
    ask = build_pipelines(Settings(use_mock=True, db_path=path), {"mock": provider})["ask"]
    return FusionTools(ask=ask, db_path=path, use_mock=True)


def panel_prompts(provider: Scripted) -> list[str]:
    return [r.user_prompt for r in provider.requests if r.metadata.get("role") == "panel"]


async def test_feedback_is_not_stored_while_the_library_is_off(
    tmp_path: Path, project: Path
) -> None:
    provider = Librarian(LEARNED)
    tools = tools_for(tmp_path, provider)
    run = await tools.fusion_ask(FusionAskInput(prompt=QUESTION), root=project)
    out = await tools.fusion_feedback(
        FeedbackInput(run_id=run["run_id"], outcome="wrong", note="x"), root=project
    )
    assert out["recorded"] is False and "library.enabled" in out["display_markdown"]
    assert not [r for r in provider.requests if r.metadata.get("role") == "librarian"]
    store = LibraryStore(db_path=tmp_path / "s.db")
    assert store.feedback(run["run_id"]) is None and store.lessons() == []
    store.close()
    await tools.aclose()


async def test_a_lesson_learned_in_run_1_is_used_in_run_2(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__LIBRARY__ENABLED", "true")
    before = tree_hash(project)
    provider = Librarian(LEARNED, {FAST: answer(("recommendation", None, "Use backoff"))})
    tools = tools_for(tmp_path, provider)

    first = await tools.fusion_ask(FusionAskInput(prompt=QUESTION), root=project)
    assert first.get("lessons_used") in (None, [])
    feedback = await tools.fusion_feedback(
        FeedbackInput(
            run_id=first["run_id"], outcome="wrong", note="It retried POST requests blindly."
        ),
        root=project,
    )
    assert feedback["recorded"] is True and feedback["action"] == "created"
    learned = feedback["lesson"]
    assert learned["verdict"] == "hurt" and learned["project_key"] == project_key(project)
    assert feedback["cost_usd"] == pytest.approx(0.0002)
    librarian = [r for r in provider.requests if r.metadata.get("role") == "librarian"]
    assert len(librarian) == 1 and "retried POST requests" in librarian[0].user_prompt

    provider.requests.clear()
    second = await tools.fusion_ask(
        FusionAskInput(prompt="Add retries to the HTTP client call in app.py"), root=project
    )
    assert second["lessons_used"] == [learned["id"]]
    assert all("Say which HTTP methods are safe" in p for p in panel_prompts(provider))
    assert learned["id"] in second["display_markdown"]

    store = LibraryStore(db_path=tmp_path / "s.db")
    assert store.lessons_used(second["run_id"]) == [learned["id"]]
    store.close()
    assert tree_hash(project) == before  # nothing written under the project root
    await tools.aclose()


async def test_ignored_without_a_note_records_feedback_but_calls_no_model(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__LIBRARY__ENABLED", "true")
    provider = Librarian(LEARNED)
    tools = tools_for(tmp_path, provider)
    run = await tools.fusion_ask(FusionAskInput(prompt=QUESTION), root=project)
    out = await tools.fusion_feedback(
        FeedbackInput(run_id=run["run_id"], outcome="ignored"), root=project
    )
    assert out["recorded"] is True and out["action"] == "none" and out["lesson"] is None
    assert not [r for r in provider.requests if r.metadata.get("role") == "librarian"]
    await tools.aclose()


async def test_feedback_on_an_unknown_run_is_an_error(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__LIBRARY__ENABLED", "true")
    tools = tools_for(tmp_path, Librarian(LEARNED))
    with pytest.raises(ValueError, match="No run"):
        await tools.fusion_feedback(FeedbackInput(run_id="nope", outcome="used"), root=project)
    await tools.aclose()


async def test_a_database_inside_the_project_turns_the_library_off(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__LIBRARY__ENABLED", "true")
    provider = Librarian(LEARNED)
    tools = tools_for(tmp_path, provider, db=project / "runs.db")
    run = await tools.fusion_ask(FusionAskInput(prompt=QUESTION), root=project)
    out = await tools.fusion_feedback(
        FeedbackInput(run_id=run["run_id"], outcome="wrong", note="x"), root=project
    )
    assert out["recorded"] is False and "inside the project" in out["display_markdown"]
    await tools.aclose()


def test_library_config_bounds_the_prompt_budget() -> None:
    with pytest.raises(ValueError):
        LibraryConfig(max_prompt_tokens=5000)


# ---------------------------------------------------------------------------------- the CLI


def test_cli_lists_shows_and_forgets_lessons(tmp_path: Path, project: Path) -> None:
    from fusion.cli.main import app

    db = tmp_path / "s.db"
    store = LibraryStore(db_path=db)
    kept = lesson(project_key=project_key(project))
    store.save(kept)
    store.save(lesson(project_key="elsewhere"))
    store.close()
    runner = CliRunner()
    base = ["library"]
    listed = runner.invoke(app, [*base, "list", "--root", str(project), "--db-path", str(db)])
    assert listed.exit_code == 0, listed.output
    assert kept.id in listed.output
    every = runner.invoke(app, [*base, "list", "--all", "--json", "--db-path", str(db)])
    assert len(json.loads(every.output)) == 2
    shown = runner.invoke(app, [*base, "show", kept.id, "--db-path", str(db)])
    assert shown.exit_code == 0 and LESSON in shown.output
    gone = runner.invoke(app, [*base, "forget", kept.id, "--db-path", str(db)])
    assert gone.exit_code == 0
    missing = runner.invoke(app, [*base, "show", kept.id, "--db-path", str(db)])
    assert missing.exit_code != 0
    wiped = runner.invoke(app, [*base, "forget", "--all", "--root", str(project), "--yes",
                                "--db-path", str(db)])
    assert wiped.exit_code == 0


def test_cli_feedback_reports_the_library_off_by_default(tmp_path: Path) -> None:
    from fusion.cli.main import app

    out = CliRunner().invoke(
        app, ["library", "feedback", "run_x", "wrong", "--mock", "--db-path", str(tmp_path / "d")]
    )
    assert out.exit_code == 0, out.output
    assert "library.enabled" in out.output


async def test_a_secret_in_the_note_is_neither_stored_nor_sent(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION__LIBRARY__ENABLED", "true")
    secret = "sk-live-0123456789abcdef0123456789"
    provider = Librarian(LEARNED)
    tools = tools_for(tmp_path, provider)
    run = await tools.fusion_ask(FusionAskInput(prompt=QUESTION), root=project)
    out = await tools.fusion_feedback(
        FeedbackInput(run_id=run["run_id"], outcome="wrong", note=f"leaked API_KEY={secret}"),
        root=project,
    )
    assert out["recorded"] is True
    sent = [r.user_prompt for r in provider.requests if r.metadata.get("role") == "librarian"]
    assert sent and all(secret not in text for text in sent)
    store = LibraryStore(db_path=tmp_path / "s.db")
    stored = store.feedback(run["run_id"])
    assert stored is not None and secret not in stored.note
    assert all(secret not in line for x in store.lessons() for line in x.evidence)
    store.close()
    await tools.aclose()
