"""Roadmap Task 17: the tool list costs the host tokens on every turn, so it has a size budget;
``fusion_plan`` replaces two tools and the old names keep working, unlisted, for one release; and
compact responses say what the host saved."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from fusion.mcp_server.schemas import PlanInput
from fusion.mcp_server.server import INSTRUCTIONS, create_mcp_server
from fusion.mcp_server.tools import FusionTools
from fusion.routing.budget import CHARS_PER_TOKEN

# What the 0.2.0 list cost: 12 tools and the server instructions, 43,776 characters of compact
# JSON (name, description, input schema) = 14,592 tokens at Fusion's 3 characters per token,
# measured 2026-11-19 at commit e4c1a9f.
TOKENS_IN_0_2_0 = 14_592
# The budget the list must stay within (measured 6,727 when set, 20,182 characters). Raising it
# is a decision: every tool schema is paid for by the host on every turn of every session,
# whether or not Fusion runs.
TOOL_LIST_BUDGET_TOKENS = 7_000
PER_TOOL_BUDGET_TOKENS = 1_000

LISTED = {
    "fusion_ask",
    "fusion_review_diff",
    "fusion_debug_error",
    "fusion_plan",
    "fusion_eval_answer",
    "fusion_digest",
    "fusion_propose_patch",
    "fusion_select_patch",
    "fusion_feedback",
    "fusion_stats",
    "fusion_compare_claude_runs",
}
ALIASES = {"fusion_decide_architecture", "fusion_plan_feature"}


@pytest.fixture
def server(tmp_path: Path) -> Any:
    return create_mcp_server(db_path=str(tmp_path / "runs.db"))


def _tokens(tool: Any) -> int:
    seen_by_the_host = {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
    }
    return int(len(json.dumps(seen_by_the_host, separators=(",", ":"))) / CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------------- the budget


async def test_the_tool_list_stays_within_its_token_budget(server: Any) -> None:
    async with Client(server) as client:
        tools = await client.list_tools()
    sizes = {t.name: _tokens(t) for t in tools}
    total = sum(sizes.values()) + int(len(INSTRUCTIONS) / CHARS_PER_TOKEN)
    assert total <= TOOL_LIST_BUDGET_TOKENS, (total, sizes)
    assert total <= TOKENS_IN_0_2_0 * 0.6  # the measured saving over 0.2.0 is at least 40%
    assert max(sizes.values()) <= PER_TOOL_BUDGET_TOKENS, sizes


async def test_the_listed_tools_are_the_merged_set_without_the_aliases(server: Any) -> None:
    async with Client(server) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == LISTED
    assert not ALIASES & names


# ----------------------------------------------------------------------------------- fusion_plan


async def test_fusion_plan_covers_both_kinds_and_keeps_their_task_types(tmp_path: Path) -> None:
    tools = FusionTools(db_path=str(tmp_path / "runs.db"))
    try:
        feature = await tools.fusion_plan(PlanInput(request="Add a dark mode toggle"))
        decision = await tools.fusion_plan(
            PlanInput(kind="decision", request="Redis or Postgres?", options=["Redis", "Postgres"])
        )
        assert tools.run_record(feature["run_id"])["task_type"] == "implementation_plan"
        assert tools.run_record(decision["run_id"])["task_type"] == "architecture_decision"
    finally:
        await tools.aclose()


def test_fusion_plan_loses_nothing_the_other_kind_would_have_used() -> None:
    """``options`` on a feature and ``existing_patterns`` on a decision reach the panel as
    background instead of being dropped."""
    feature = PlanInput(request="Add a cache", options=["in-process LRU"], paths=["a.py"])
    as_feature = feature.as_feature()
    assert as_feature.feature_description == "Add a cache" and as_feature.paths == ["a.py"]
    assert "in-process LRU" in as_feature.context
    decision = PlanInput(
        kind="decision", request="Pick a queue", existing_patterns="every job is idempotent"
    )
    as_decision = decision.as_decision()
    assert as_decision.question == "Pick a queue"
    assert "every job is idempotent" in as_decision.context


@pytest.mark.parametrize(
    ("alias", "arguments", "replacement"),
    [
        (
            "fusion_plan_feature",
            {"feature_description": "Add a dark mode toggle"},
            "fusion_plan",
        ),
        (
            "fusion_decide_architecture",
            {"question": "Redis or Postgres?", "options": ["Redis", "Postgres"]},
            "fusion_plan",
        ),
    ],
)
async def test_the_old_names_still_answer_and_say_what_to_call_instead(
    server: Any, alias: str, arguments: dict[str, Any], replacement: str
) -> None:
    async with Client(server) as client:
        result = await client.call_tool(alias, {"input": arguments})
    record = result.structured_content
    assert record is not None and record["run_id"]
    assert any(replacement in w and "deprecated" in w for w in record["warnings"]), record[
        "warnings"
    ]


async def test_fusion_plan_through_the_server_returns_a_run(server: Any) -> None:
    async with Client(server) as client:
        result = await client.call_tool(
            "fusion_plan",
            {"input": {"kind": "decision", "request": "Redis or Postgres?"}},
        )
    record = result.structured_content
    assert record is not None and record["details_uri"].startswith("fusion://runs/")
    assert not any("deprecated" in w for w in record["warnings"])


def test_a_plan_without_a_request_is_refused() -> None:
    with pytest.raises(ValueError, match="request"):
        PlanInput.model_validate({"kind": "feature"})


# ------------------------------------------------------------------------- compact responses


async def test_compact_text_says_how_many_host_tokens_were_saved(server: Any) -> None:
    async with Client(server) as client:
        result = await client.call_tool(
            "fusion_digest", {"input": {"question": "What is this?", "paths": ["pyproject.toml"]}}
        )
    record = result.structured_content
    assert record is not None
    saved = record["host_tokens_saved_estimate"]
    assert saved and saved > 0
    text = result.content[0].text  # type: ignore[union-attr]
    assert f"~{saved:,} host tokens saved" in text
    assert record["confidence"] is not None and record["cost_usd"] is not None


async def test_a_call_that_read_nothing_claims_no_saving(server: Any) -> None:
    async with Client(server) as client:
        result = await client.call_tool("fusion_ask", {"input": {"prompt": "What is a mutex?"}})
    assert "host tokens saved" not in result.content[0].text  # type: ignore[union-attr]
