"""Smoke test the real FastMCP server in-process (guards dependency upgrades)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from fusion.mcp_server.server import create_mcp_server

EXPECTED_TOOLS = {
    "fusion_ask",
    "fusion_review_diff",
    "fusion_debug_error",
    "fusion_decide_architecture",
    "fusion_plan_feature",
    "fusion_eval_answer",
    "fusion_stats",
    "fusion_compare_claude_runs",
}


async def test_server_lists_all_tools(tmp_path: Path) -> None:
    server = create_mcp_server(db_path=str(tmp_path / "runs.db"))
    async with Client(server) as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS


async def test_fusion_ask_round_trip_with_mock_provider(tmp_path: Path) -> None:
    server = create_mcp_server(db_path=str(tmp_path / "runs.db"))
    async with Client(server) as client:
        result = await client.call_tool(
            "fusion_ask",
            {"input": {"prompt": "How should I retry a failed HTTP call?", "context": "httpx"}},
        )
    data = result.data
    assert isinstance(data, dict)
    assert data["run_id"]
    assert "display_markdown" in data
    assert "usage" in data


async def test_mock_env_never_reaches_real_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FUSION_DEFAULT_PROVIDER=mock must keep every call offline even if keys exist."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    server = create_mcp_server(db_path=str(tmp_path / "runs.db"))
    async with Client(server) as client:
        result = await client.call_tool(
            "fusion_ask", {"input": {"prompt": "Explain idempotent retries", "context": "http"}}
        )
    providers = {call["provider"] for call in result.data["usage"]["per_model"]}
    assert providers == {"mock"}
