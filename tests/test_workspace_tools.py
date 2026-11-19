"""Workspace-aware tool inputs and ``fusion_digest`` (roadmap Task 5), through a real MCP client.

The host names files (``paths``, ``globs``) or a diff (``diff_ref``) instead of pasting them; the
server reads them under the client's root (or its cwd) and says what it read and skipped.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from fusion.mcp_server.schemas import (
    DebugErrorInput,
    DigestInput,
    FusionAskInput,
    PlanFeatureInput,
    ReviewDiffInput,
)
from fusion.mcp_server.server import client_root, create_mcp_server
from fusion.mcp_server.tools import FusionTools
from fusion.routing.budget import CHARS_PER_TOKEN
from fusion.storage.run_store import RunStore

FAKE_KEY = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"


def _git(root: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid"}
    env |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def main():\n    return 1\n")
    (root / "src" / "util.py").write_text("def helper():\n    return 2\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "first")
    (root / "src" / "app.py").write_text("def main():\n    return 42\n")
    _git(root, "commit", "-q", "-am", "second")
    (root / ".env").write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n")
    return root


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "runs.db")


def _stored_input(db: str, run_id: str) -> dict[str, Any]:
    with RunStore(db_path=db) as store:
        record = store.get_run(run_id)
    assert record is not None
    return record.input_data


# ------------------------------------------------------------------------------------- schemas


def test_review_takes_a_diff_or_a_diff_ref() -> None:
    assert ReviewDiffInput(diff="+ a").diff == "+ a"  # the 0.2 form still works
    assert ReviewDiffInput(diff_ref="HEAD~1").diff == ""
    with pytest.raises(ValueError, match="diff_ref"):
        ReviewDiffInput()


def test_every_panel_tool_accepts_paths_and_globs() -> None:
    for model in (FusionAskInput, ReviewDiffInput, DebugErrorInput, PlanFeatureInput):
        fields = model.model_fields
        assert {"paths", "globs", "diff_ref"} <= set(fields), model.__name__


def test_digest_needs_material_to_read() -> None:
    with pytest.raises(ValueError, match="paths"):
        DigestInput(question="what does it do?")
    assert DigestInput(question="q", globs=["src/*.py"]).max_answer_tokens == 600


# ------------------------------------------------------------------ the done-when of Task 5


async def test_review_by_diff_ref_reviews_without_the_host_sending_the_diff(
    repo: Path, db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)  # Claude Code starts a stdio server in the project directory
    async with Client(create_mcp_server(db_path=db)) as client:
        result = await client.call_tool(
            "fusion_review_diff", {"input": {"diff_ref": "HEAD~1", "detail": "full"}}
        )
    record = result.structured_content
    assert record is not None
    assert record["host_tokens_saved_estimate"] > 0
    assert record["workspace"]["diff_ref"] == "HEAD~1"
    primary = _stored_input(db, record["run_id"])["primary_content"]
    assert "+    return 42" in primary and "-    return 1" in primary


async def test_fusion_read_root_names_the_root_when_the_client_gives_none(
    repo: Path, db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FUSION_READ_ROOT", str(repo))
    async with Client(create_mcp_server(db_path=db)) as client:
        result = await client.call_tool(
            "fusion_ask", {"input": {"prompt": "?", "paths": ["src/util.py"]}}
        )
    record = result.structured_content
    assert record is not None
    snippets = _stored_input(db, record["run_id"])["file_snippets"]
    assert any(s.startswith("src/util.py\n") and "return 2" in s for s in snippets)


async def test_secret_and_outside_paths_are_skipped_with_a_warning_and_never_stored(
    repo: Path, db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    async with Client(create_mcp_server(db_path=db)) as client:
        result = await client.call_tool(
            "fusion_ask",
            {"input": {"prompt": "?", "paths": [".env", "../outside.txt", "src/app.py"]}},
        )
        record = result.structured_content
        assert record is not None
        stored = json.loads(
            (await client.read_resource(record["details_uri"]))[0].text  # type: ignore[union-attr]
        )
    warning = next(w for w in record["warnings"] if w.startswith("Workspace"))
    assert ".env" in warning and "../outside.txt" in warning
    assert [f["path"] for f in stored["workspace"]["read"]] == ["src/app.py"]
    assert {s["path"] for s in stored["workspace"]["skipped"]} == {".env", "../outside.txt"}
    with RunStore(db_path=db) as store:
        run = store.get_run(record["run_id"])
    assert run is not None
    assert FAKE_KEY not in json.dumps(run.input_data) + json.dumps(run.sanitized_input)


async def test_a_bad_diff_ref_is_a_tool_error_that_names_the_problem(
    repo: Path, db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    async with Client(create_mcp_server(db_path=db)) as client:
        with pytest.raises(ToolError, match="Not a git ref"):
            await client.call_tool("fusion_review_diff", {"input": {"diff_ref": "--output=x"}})


# ---------------------------------------------------------------------------- client roots


class _Session:
    def __init__(self, roots: list[str], *, declares: bool = True, channel: bool = True) -> None:
        self.roots, self.declares, self.can_send_request = roots, declares, channel

    def check_client_capability(self, capability: Any) -> bool:
        return self.declares

    async def list_roots(self) -> Any:
        from mcp.types import ListRootsResult, Root

        if not self.roots:
            raise RuntimeError("client error")
        return ListRootsResult(roots=[Root(uri=uri) for uri in self.roots])  # type: ignore[arg-type]


class _Ctx:
    def __init__(self, session: _Session) -> None:
        self.session = session


async def test_the_first_file_root_of_the_client_is_the_project_root(tmp_path: Path) -> None:
    uri = (tmp_path / "my project").as_uri()
    root = await client_root(_Ctx(_Session([uri, "file:///elsewhere"])))
    assert root == tmp_path / "my project"


@pytest.mark.parametrize(
    "session",
    [
        _Session(["file:///x"], declares=False),
        _Session(["file:///x"], channel=False),
        _Session([]),  # the client errors
    ],
)
async def test_no_usable_client_root_falls_back(session: _Session) -> None:
    assert await client_root(_Ctx(session)) is None


# ------------------------------------------------------------------------------------ digest


async def test_digest_reads_globs_and_answers_small(
    repo: Path, db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    async with Client(create_mcp_server(db_path=db)) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert "fusion_digest" in tools
        result = await client.call_tool(
            "fusion_digest",
            {
                "input": {
                    "question": "What do these return?",
                    "globs": ["src/*.py"],
                    "max_answer_tokens": 100,
                }
            },
        )
    record = result.structured_content
    assert record is not None
    assert record["host_tokens_saved_estimate"] > 0
    assert len(record["display_markdown"]) <= (100 + 200) * CHARS_PER_TOKEN + 300
    snippets = _stored_input(db, record["run_id"])["file_snippets"]
    assert {s.split("\n", 1)[0] for s in snippets} == {"src/app.py", "src/util.py"}


async def test_digest_asks_for_a_short_answer(repo: Path, tmp_path: Path) -> None:
    from _scripted import Scripted
    from fusion.orchestration.factory import Settings, build_pipelines

    provider = Scripted()
    pipes = build_pipelines(
        Settings(use_mock=True, db_path=str(tmp_path / "p.db")), {"mock": provider}
    )
    tools = FusionTools(db_path=str(tmp_path / "t.db"), use_mock=True, ask=pipes["ask"])
    await tools.fusion_digest(
        DigestInput(question="Q?", paths=["src/app.py"], max_answer_tokens=300), root=repo
    )
    prompt = provider.requests[0].user_prompt
    assert "300 tokens" in prompt and "return 42" in prompt
    await tools.aclose()


# ------------------------------------------------------------------------------------- stats


async def test_stats_sum_the_host_tokens_saved(repo: Path, db: str) -> None:
    tools = FusionTools(db_path=db, use_mock=True)
    first = await tools.fusion_ask(FusionAskInput(prompt="a?", paths=["src/app.py"]), root=repo)
    second = await tools.fusion_ask(FusionAskInput(prompt="b?", paths=["src/util.py"]), root=repo)
    saved = first["host_tokens_saved_estimate"] + second["host_tokens_saved_estimate"]
    assert saved > 0
    stats = await tools.run_store.aget_stats()
    assert stats.host_tokens_saved_estimate == saved
    from fusion.mcp_server.schemas import FusionStatsInput

    shown = await tools.fusion_stats(FusionStatsInput())
    assert shown["result"]["host_tokens_saved_estimate"] == saved
    assert "Host tokens saved" in shown["display_markdown"]
    await tools.aclose()


async def test_a_call_that_pastes_everything_reports_no_saving(db: str) -> None:
    tools = FusionTools(db_path=db, use_mock=True)
    output = await tools.fusion_ask(FusionAskInput(prompt="?"))
    assert output.get("host_tokens_saved_estimate") in (None, 0)
    await tools.aclose()


# ------------------------------------------------------------------------------- descriptions


async def test_descriptions_tell_the_host_to_name_files_not_paste_them(db: str) -> None:
    async with Client(create_mcp_server(db_path=db)) as client:
        tools = {t.name: t for t in await client.list_tools()}
    review = tools["fusion_review_diff"].description or ""
    assert "diff_ref" in review and "paths" in review
    for name in ("fusion_ask", "fusion_debug_error", "fusion_plan"):
        assert "paths" in (tools[name].description or ""), name
    assert "reads no files" not in review


# ---------------------------------------------------------------------------------------- CLI


def _cli(args: list[str]) -> Any:
    from typer.testing import CliRunner

    from fusion.cli.main import app

    return CliRunner().invoke(app, [*args, "--mock", "--json", "-q"])


def test_cli_ask_reads_attached_files_through_the_workspace_reader(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    result = _cli(["ask", "What does main return?", "-f", "src/app.py", "-f", ".env"])
    assert result.exit_code == 0, result.output
    record = json.loads(result.stdout)
    assert record["host_tokens_saved_estimate"] > 0
    assert any(".env" in w and "secret" in w for w in record["warnings"])


def test_cli_review_diff_takes_a_diff_ref(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    result = _cli(["review-diff", "--diff-ref", "HEAD~1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["host_tokens_saved_estimate"] > 0


def test_cli_digest_reads_globs(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    result = _cli(["digest", "What do these files do?", "--glob", "src/*.py"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["host_tokens_saved_estimate"] > 0


def test_cli_a_bad_diff_ref_is_one_line(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    result = _cli(["review-diff", "--diff-ref", "no-such-branch"])
    assert result.exit_code == 1 and "git diff no-such-branch failed" in result.stderr
    assert "Traceback" not in result.output


async def test_review_focus_2_refused_paths_never_reach_a_model(repo: Path, tmp_path: Path) -> None:
    """`.env`, a symlink out of the repo, a binary and a 50 MB file: skipped, never sent."""
    from _scripted import Scripted
    from fusion.orchestration.factory import Settings, build_pipelines

    outside = tmp_path / "outside-secret.txt"
    outside.write_text("OUTSIDE-MARKER")
    (repo / "escape.txt").symlink_to(outside)
    (repo / "blob.bin").write_bytes(b"BINARY-MARKER\x00\x01\x02")
    with (repo / "huge.log.txt").open("wb") as handle:
        handle.truncate(50 * 1024 * 1024)
    provider = Scripted()
    pipes = build_pipelines(
        Settings(use_mock=True, db_path=str(tmp_path / "p.db")), {"mock": provider}
    )
    tools = FusionTools(db_path=str(tmp_path / "t.db"), use_mock=True, ask=pipes["ask"])
    refused = [".env", "escape.txt", "blob.bin", "huge.log.txt"]
    output = await tools.fusion_ask(
        FusionAskInput(
            prompt="What does helper() in src/util.py return, and why?",
            paths=[*refused, "src/util.py"],
        ),
        root=repo,
    )
    await tools.aclose()
    sent = "\n".join(r.system_prompt + r.user_prompt for r in provider.requests)
    assert "return 2" in sent  # the readable file did go
    for marker in (FAKE_KEY, "OUTSIDE-MARKER", "BINARY-MARKER", "ANTHROPIC_API_KEY"):
        assert marker not in sent, marker
    assert {s["path"] for s in output["workspace"]["skipped"]} == set(refused)
