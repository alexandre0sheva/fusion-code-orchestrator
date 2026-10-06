"""The MCP server's surface: tool metadata, compact responses, progress, resources and prompts.

Everything runs offline against the mock provider, in-process through a real FastMCP client.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from fastmcp import Client

from fusion.mcp_server.response import COMPACT_MAX_TOKENS, cap_markdown, present_run
from fusion.mcp_server.schemas import (
    DebugErrorInput,
    FusionAskInput,
    ReviewDiffInput,
)
from fusion.mcp_server.server import create_mcp_server
from fusion.mcp_server.tools import DEFAULT_SOFT_TIMEOUT_S, soft_timeout_from_env
from fusion.routing.budget import CHARS_PER_TOKEN

ARGUMENTS: dict[str, dict[str, Any]] = {
    "fusion_ask": {"prompt": "How should I retry a failed HTTP call in httpx?"},
    "fusion_review_diff": {"diff": "+ query = 'SELECT * FROM t WHERE id=' + uid", "context": "py"},
    "fusion_debug_error": {"error_message": "KeyError: 'id'", "stack_trace": "File x.py, line 3"},
    "fusion_decide_architecture": {
        "question": "Redis or Postgres for a job queue?",
        "options": ["Redis", "Postgres"],
    },
    "fusion_plan_feature": {"feature_description": "Add a dark mode toggle to the settings page"},
    "fusion_eval_answer": {"question": "What is 2+2?", "answer": "4"},
    "fusion_stats": {},
    "fusion_compare_claude_runs": {
        "task_prompt": "Fix the bug",
        "opus_output": "Changed the loop bound.",
        "fusion_output": "Changed the loop bound and added a test.",
    },
}


@pytest.fixture
def server(tmp_path: Path) -> Any:
    return create_mcp_server(db_path=str(tmp_path / "runs.db"))


# ------------------------------------------------------------------------------ tool metadata


async def test_every_tool_declares_annotations_and_a_typed_output_schema(server: Any) -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == set(ARGUMENTS)
    for name, tool in tools.items():
        hints = tool.annotations
        assert hints is not None, name
        assert hints.read_only_hint is True and hints.destructive_hint is False, name
        assert hints.open_world_hint is (name != "fusion_stats"), name
        assert hints.idempotent_hint is (name == "fusion_stats"), name
        assert tool.output_schema and "display_markdown" in tool.output_schema["properties"], name


async def test_descriptions_say_when_to_call_and_when_not_to(server: Any) -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in set(ARGUMENTS) - {"fusion_stats"}:
        text = tools[name].description or ""
        assert ("Call it" in text and "Do not call it" in text) or "Only for" in text, name
    for name in ("fusion_ask", "fusion_review_diff", "fusion_debug_error"):
        description = tools[name].description or ""
        assert "tens of seconds" in description and "cents" in description, name
    assert "no model" in (tools["fusion_stats"].description or "")


async def test_schemas_offer_the_cost_cap_strategy_and_detail_but_hide_legacy_names(
    server: Any,
) -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
    review = tools["fusion_review_diff"].input_schema["properties"]["input"]["properties"]
    assert {"strategy", "detail", "max_cost_usd", "context"} <= set(review)
    assert "repo_context" not in review and "repo_summary" not in review
    debug = tools["fusion_debug_error"].input_schema["properties"]["input"]["properties"]
    assert "code_context" not in debug and "stack_trace" in debug


# ------------------------------------------------------------------------------ the responses


async def test_each_tool_answers_with_text_and_a_record_that_matches_its_schema(
    server: Any,
) -> None:
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        for name, arguments in ARGUMENTS.items():
            result = await client.call_tool(name, {"input": arguments})
            record = result.structured_content
            assert record is not None, name
            assert result.content[0].text == record["display_markdown"], name  # type: ignore[union-attr]
            jsonschema.validate(record, tools[name].output_schema)


async def test_compact_is_the_default_and_full_adds_the_rest(server: Any) -> None:
    async with Client(server) as client:
        compact = (
            await client.call_tool("fusion_review_diff", {"input": ARGUMENTS["fusion_review_diff"]})
        ).structured_content
        full_args = {**ARGUMENTS["fusion_review_diff"], "detail": "full"}
        full = (
            await client.call_tool("fusion_review_diff", {"input": full_args})
        ).structured_content
    assert compact is not None and full is not None
    assert {"result", "claims", "usage", "routing", "evals"}.isdisjoint(compact)
    assert {"result", "claims", "agreement", "usage", "routing", "evals"} <= set(full)
    assert compact["run_id"] and compact["details_uri"] == f"fusion://runs/{compact['run_id']}"
    assert len(json.dumps(compact)) < len(json.dumps(full)) / 2


def test_a_long_answer_is_cut_to_the_token_ceiling_and_points_to_the_run() -> None:
    long = "\n".join(f"line {i} of a very long answer" for i in range(5000))
    cut = cap_markdown(long, "run-1")
    assert len(cut) <= COMPACT_MAX_TOKENS * CHARS_PER_TOKEN + 300
    assert "fusion://runs/run-1" in cut and cut.startswith("line 0")
    short = "a short answer"
    assert cap_markdown(short, "run-1") == short


def test_a_cost_that_is_not_known_is_reported_as_missing_not_zero() -> None:
    output = {
        "run_id": "r",
        "display_markdown": "x",
        "cost_latency": {
            "total_cost_usd": 0.0,
            "total_cost_known": False,
            "total_latency_ms": 1500,
        },
        "usage": {"successful_model_calls": 2},
        "routing": {"strategy": "panel-duo"},
    }
    record = present_run(output, "compact")
    assert record.cost_usd is None and record.latency_s == 1.5 and record.models_called == 2


# ------------------------------------------------------------------------------ input aliases


def test_older_context_names_fold_into_context() -> None:
    old = ReviewDiffInput.model_validate({"diff": "d", "repo_context": "R", "repo_summary": "S"})
    assert old.context == "R\n\nS"
    both = ReviewDiffInput.model_validate({"diff": "d", "context": "C", "repo_summary": "S"})
    assert both.context == "C\n\nS"
    same = ReviewDiffInput.model_validate({"diff": "d", "context": "C", "repo_context": "C"})
    assert same.context == "C"
    debug = DebugErrorInput.model_validate({"error_message": "e", "code_context": "def f(): ..."})
    assert debug.context == "def f(): ..."
    assert FusionAskInput.model_validate({"prompt": "p"}).context == ""


def test_a_cost_cap_must_be_positive() -> None:
    assert FusionAskInput(prompt="p", max_cost_usd=0.05).max_cost_usd == 0.05
    with pytest.raises(ValueError, match="greater than 0"):
        FusionAskInput(prompt="p", max_cost_usd=0)


async def test_every_context_field_reaches_the_models(tmp_path: Path) -> None:
    """Review used to drop `context` and `file_snippets`; debug dropped the stack trace."""
    from _scripted import Scripted
    from fusion.mcp_server.tools import FusionTools
    from fusion.orchestration.factory import Settings, build_pipelines

    provider = Scripted()
    pipes = build_pipelines(
        Settings(use_mock=True, db_path=str(tmp_path / "p.db")), {"mock": provider}
    )
    tools = FusionTools(
        db_path=str(tmp_path / "t.db"),
        use_mock=True,
        code_review=pipes["code_review"],
        debug=pipes["debug"],
    )
    await tools.fusion_review_diff(
        ReviewDiffInput(
            diff="+ a = 1",
            repo_summary="LEGACY-SUMMARY",
            context="NEW-CONTEXT",
            file_snippets=["app.py: SNIPPET-TEXT"],
        )
    )
    prompt = provider.requests[0].user_prompt
    assert "LEGACY-SUMMARY" in prompt and "NEW-CONTEXT" in prompt and "SNIPPET-TEXT" in prompt
    provider.requests.clear()
    await tools.fusion_debug_error(
        DebugErrorInput(error_message="boom", stack_trace="TRACE-LINE", context="CTX")
    )
    prompt = provider.requests[0].user_prompt
    assert "TRACE-LINE" in prompt and "CTX" in prompt
    await tools.aclose()


# ------------------------------------------------------------------------------ progress


async def test_progress_messages_arrive_in_order_and_only_increase(server: Any) -> None:
    seen: list[tuple[float, float | None, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        seen.append((progress, total, message))

    async with Client(server) as client:
        await client.call_tool(
            "fusion_ask",
            {"input": {"prompt": "How do I retry an HTTP call?", "strategy": "panel-cheap"}},
            progress_handler=on_progress,
        )
    messages = [m or "" for _, _, m in seen]
    assert messages[0] == "routing"
    assert "panel: asking 3 models" in messages
    done = [m for m in messages if m.startswith("panel ") and m.endswith(" done")]
    assert done == ["panel 1/3 done", "panel 2/3 done", "panel 3/3 done"]
    order = [messages.index(m) for m in ("panel: asking 3 models", done[0], done[-1])]
    assert order == sorted(order)
    assert messages.index("synthesizing") > messages.index(done[-1])
    assert messages[-1] == "checking the final answer"
    values = [p for p, _, _ in seen]
    assert values == sorted(set(values))


async def test_a_call_without_a_progress_handler_still_works(server: Any) -> None:
    async with Client(server) as client:
        result = await client.call_tool("fusion_ask", {"input": ARGUMENTS["fusion_ask"]})
    assert result.structured_content is not None


# ------------------------------------------------------------------------------ resources


async def test_a_run_can_be_read_back_by_the_id_in_the_response(server: Any) -> None:
    async with Client(server) as client:
        call = await client.call_tool(
            "fusion_review_diff", {"input": ARGUMENTS["fusion_review_diff"]}
        )
        record = call.structured_content
        assert record is not None
        contents = await client.read_resource(record["details_uri"])
        stored = json.loads(contents[0].text)  # type: ignore[union-attr]
        assert stored["run_id"] == record["run_id"] and stored["status"] == "completed"
        assert stored["output"]["claims"] and stored["steps"]
        assert "input" not in stored  # what the caller sent is not echoed back
        with pytest.raises(Exception, match="No run 'nope'"):
            await client.read_resource("fusion://runs/nope")


async def test_stats_and_strategies_are_resources(server: Any) -> None:
    async with Client(server) as client:
        resources = {str(r.uri) for r in await client.list_resources()}
        assert resources == {"fusion://stats", "fusion://strategies"}
        templates = {t.uri_template for t in await client.list_resource_templates()}
        assert templates == {"fusion://runs/{run_id}"}
        strategies = json.loads((await client.read_resource("fusion://strategies"))[0].text)  # type: ignore[union-attr]
        stats = (await client.read_resource("fusion://stats"))[0].text  # type: ignore[union-attr]
    names = {s["name"] for s in strategies["strategies"]}
    assert {"solo-cheap", "panel-duo", "panel-digest"} <= names
    assert strategies["budget_presets"]["medium"] in names
    assert stats.strip()


# ------------------------------------------------------------------------------ prompts


async def test_the_prompts_name_the_tool_to_call(server: Any) -> None:
    async with Client(server) as client:
        listed = {p.name for p in await client.list_prompts()}
        assert listed == {"review-this-diff", "debug-this-error", "plan-this-feature"}
        review = await client.get_prompt("review-this-diff", {"diff": "+ x", "focus": "security"})
        debug = await client.get_prompt("debug-this-error", {"error": "KeyError"})
        plan = await client.get_prompt("plan-this-feature", {"feature": "dark mode"})
        bare = await client.get_prompt("review-this-diff", {})
    text = lambda r: r.messages[0].content.text  # noqa: E731
    assert "fusion_review_diff" in text(review) and "+ x" in text(review)
    assert "security" in text(review)
    assert "fusion_debug_error" in text(debug) and "KeyError" in text(debug)
    assert "fusion_plan_feature" in text(plan) and "dark mode" in text(plan)
    assert "git diff" in text(bare)


# ------------------------------------------------------------------------------ the soft limit


def test_the_soft_timeout_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FUSION_TOOL_SOFT_TIMEOUT_S", raising=False)
    assert soft_timeout_from_env() == DEFAULT_SOFT_TIMEOUT_S == 90.0
    monkeypatch.setenv("FUSION_TOOL_SOFT_TIMEOUT_S", "12.5")
    assert soft_timeout_from_env() == 12.5
    for off in ("0", "off", "none"):
        monkeypatch.setenv("FUSION_TOOL_SOFT_TIMEOUT_S", off)
        assert soft_timeout_from_env() is None
    monkeypatch.setenv("FUSION_TOOL_SOFT_TIMEOUT_S", "soon")
    assert soft_timeout_from_env() == DEFAULT_SOFT_TIMEOUT_S
    captured = capsys.readouterr()
    assert captured.out == "" and "FUSION_TOOL_SOFT_TIMEOUT_S" in captured.err


async def test_every_tool_pipeline_gets_the_soft_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fusion.mcp_server.tools import FusionTools

    monkeypatch.setenv("FUSION_TOOL_SOFT_TIMEOUT_S", "33")
    tools = FusionTools(db_path=str(tmp_path / "t.db"), use_mock=True)
    pipelines = [tools._code_review, tools._ask, tools._debug, tools._architecture, tools._plan]
    assert {p.soft_timeout_s for p in pipelines} == {33.0}
    await tools.aclose()


async def test_stdio_server_writes_only_json_rpc_to_stdout(tmp_path: Path) -> None:
    """stdout is the protocol channel: one stray print would corrupt every client."""
    import os
    import sys

    env = {
        **os.environ,
        "FUSION_DEFAULT_PROVIDER": "mock",
        "FUSION_DB_PATH": str(tmp_path / "s.db"),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "fusion.main",
        "mcp",
        "--db-path",
        str(tmp_path / "s.db"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert process.stdin is not None and process.stdout is not None

    def line(payload: dict[str, Any]) -> bytes:
        return (json.dumps({"jsonrpc": "2.0", **payload}) + "\n").encode()

    process.stdin.write(
        line(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            }
        )
        + line({"method": "notifications/initialized"})
        + line(
            {
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "fusion_ask",
                    "arguments": {"input": {"prompt": "How do I retry an HTTP call?"}},
                    "_meta": {"progressToken": "p1"},
                },
            }
        )
    )
    await process.stdin.drain()
    lines: list[str] = []
    try:
        async with asyncio.timeout(60):
            while True:
                raw = (await process.stdout.readline()).decode()
                assert raw, "the server closed stdout before answering"
                lines.append(raw)
                if json.loads(raw).get("id") == 2:
                    break
    finally:
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:
            process.kill()
    parsed = [json.loads(raw) for raw in lines]  # any non-JSON line fails here
    assert all(message.get("jsonrpc") == "2.0" for message in parsed)
    progress = [m for m in parsed if m.get("method") == "notifications/progress"]
    assert progress and progress[0]["params"]["progressToken"] == "p1"
    answer = next(m for m in parsed if m.get("id") == 2)
    assert not answer["result"].get("isError") and answer["result"]["structuredContent"]["run_id"]
