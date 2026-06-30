"""FastMCP server setup and registration."""

from __future__ import annotations

from fusion.mcp_server.schemas import (
    CompareImplementInput,
    DebugErrorInput,
    DecideArchitectureInput,
    EvalAnswerInput,
    PlanFeatureInput,
    ReviewDiffInput,
)
from fusion.mcp_server.tools import FusionTools


def create_mcp_server(*, db_path: str | None = None):
    """Create and configure the FastMCP server with all fusion tools."""
    from fastmcp import FastMCP

    mcp = FastMCP(
        name="fusion-code-orchestrator",
        instructions=(
            "Multi-model orchestration engine for code review, debugging, "
            "architecture decisions, implementation planning, and answer evaluation."
        ),
    )

    tools = FusionTools(db_path=db_path)

    @mcp.tool()
    async def fusion_review_diff(input: ReviewDiffInput) -> dict:
        """Review a code diff with multi-model panel evaluation and synthesis."""
        return await tools.fusion_review_diff(input)

    @mcp.tool()
    async def fusion_debug_error(input: DebugErrorInput) -> dict:
        """Debug an error with multi-model analysis and fix recommendations."""
        return await tools.fusion_debug_error(input)

    @mcp.tool()
    async def fusion_decide_architecture(input: DecideArchitectureInput) -> dict:
        """Evaluate architecture options with multi-model consensus."""
        return await tools.fusion_decide_architecture(input)

    @mcp.tool()
    async def fusion_plan_feature(input: PlanFeatureInput) -> dict:
        """Create an implementation plan with multi-model review."""
        return await tools.fusion_plan_feature(input)

    @mcp.tool()
    async def fusion_eval_answer(input: EvalAnswerInput) -> dict:
        """Evaluate answer quality with multi-model scoring."""
        return await tools.fusion_eval_answer(input)

    @mcp.tool()
    async def fusion_compare_implement(input: CompareImplementInput) -> dict:
        """Implement a task twice (Opus vs Fusion) and return cost, latency, and results."""
        return await tools.fusion_compare_implement(input)

    return mcp


def run_server(*, db_path: str | None = None) -> None:
    """Run the MCP server using stdio transport."""
    from fusion.config.env import load_env

    load_env()
    mcp = create_mcp_server(db_path=db_path)
    # stdout is reserved for JSON-RPC; never print banners or logs there.
    mcp.run(show_banner=False)
