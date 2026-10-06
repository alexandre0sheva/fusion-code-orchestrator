"""FastMCP server setup and registration.

Tool functions are annotated with ``Context``, which FastMCP finds from the annotation, so this
module deliberately has no ``from __future__ import annotations`` and imports FastMCP inside the
factory: the CLI must not load FastMCP just to print help.
"""

import asyncio
import ipaddress
import itertools
import json
import sys
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, asynccontextmanager
from typing import Any, Literal

from fusion.mcp_server.response import (
    present_comparison,
    present_run,
    present_stats,
    to_tool_result,
)
from fusion.mcp_server.schemas import (
    CompareClaudeRunsInput,
    CompareToolResult,
    DebugErrorInput,
    DecideArchitectureInput,
    EvalAnswerInput,
    FusionAskInput,
    FusionStatsInput,
    FusionToolResult,
    PlanFeatureInput,
    ReviewDiffInput,
    StatsToolResult,
)
from fusion.mcp_server.tools import FusionTools
from fusion.orchestration.progress import progress_sink
from fusion.security.output import harden

Transport = Literal["stdio", "http"]
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765

INSTRUCTIONS = (
    "Fusion asks a panel of cheap models and returns text; you stay the executor and apply edits "
    "yourself. Use it for a second opinion on review, debugging, architecture, planning and "
    "hard coding questions, not for trivial edits. Each call takes tens of seconds and costs "
    "cents; progress is reported while it runs. Responses are compact: the answer, the top "
    "claims, confidence and one cost line. Pass detail='full' for everything, or read "
    "fusion://runs/{run_id} later. Pass max_cost_usd to cap spend and strategy to pick a "
    "cheaper or faster form (see fusion://strategies). With the panel-digest strategy no model "
    "merges the panel's answers: the response lists the shared, disputed and single-model "
    "points and each model's answer, and you decide what to keep. If a call runs past the soft "
    "time limit it returns the panel's digest and says so."
)

_TEXT_ONLY = (
    " Fusion sees only the text you send and returns text: it reads no files and runs nothing, "
    "so check its claims against the code before editing. A call takes tens of seconds (a panel "
    "is slower than one model) and costs cents; the response states the cost."
)
_VERIFY = (
    _TEXT_ONLY + " Use strategy='solo-cheap' for the quickest answer or max_cost_usd to cap spend."
)

ASK_DESCRIPTION = (
    "Ask a panel of cheap models a self-contained coding question or task and get one answer "
    "with the points the models agree on. Call it for a second opinion on a hard implementation "
    "question when a wrong answer would be costly, or when you are unsure. Do not call it for "
    "trivial edits, for questions the code in front of you already answers, or for anything "
    "that needs files read or commands run." + _VERIFY
)
REVIEW_DESCRIPTION = (
    "Review a code diff with a panel of cheap models and get findings grouped by how many "
    "models agree. Call it before committing or merging a non-trivial or risky change "
    "(security, concurrency, migrations, public APIs), or for an independent check of your own "
    "patch. Do not call it for typo, formatting or one-line changes. Send the unified diff in "
    "`diff` and background in `context`." + _VERIFY
)
DEBUG_DESCRIPTION = (
    "Rank the likely root causes of an error and get steps to verify each one and a minimal fix "
    "strategy. Call it when you have an error message or stack trace and the cause is not "
    "obvious after a first look, or after a fix attempt failed. Do not call it when the message "
    "already names the cause (a missing import, a typo)." + _VERIFY
)
ARCHITECTURE_DESCRIPTION = (
    "Compare design options and get a recommendation with tradeoffs, risks, reversibility and "
    "a migration plan. Call it before committing to a choice that is hard to reverse (storage, "
    "queues, framework, service boundaries). Do not call it for choices that are easy to "
    "reverse or that project conventions already settle." + _VERIFY
)
PLAN_DESCRIPTION = (
    "Draft an implementation plan for a feature: the sequence of steps, affected modules, API "
    "and data changes, tests to add, risks and open questions. Call it at the start of a "
    "multi-file feature when the approach is not obvious. Do not call it for small changes you "
    "can already sketch." + _VERIFY
)
EVAL_DESCRIPTION = (
    "Score an answer against its question and a rubric: strengths, weaknesses, unsupported "
    "claims and missing points. Call it to check a draft answer or explanation before you act on "
    "it or present it. Do not call it to find out whether code works or a fact is true: it "
    "judges only the text you pass in and can run nothing." + _TEXT_ONLY
)
STATS_DESCRIPTION = (
    "Show cumulative Fusion usage: money spent against the baseline model's estimated cost, "
    "savings, call counts and the shadow A/B win-rate. Free and instant: it reads the local "
    "run database and calls no model. Call it when asked whether Fusion is paying off."
)
COMPARE_DESCRIPTION = (
    "Score two finished Claude Code outputs for the same task (one made with Opus alone, one "
    "with Fusion) against the same rubric, and report which is better, cheaper and faster. "
    "Only for measuring Fusion against a baseline when the user asks; do not use it in normal "
    "work. It runs two evaluation panels, so it costs more than other tools."
)


def _progress_to(ctx: Any) -> AbstractContextManager[None]:
    """Forward the run's progress messages to the client (a no-op without a progress token)."""
    counter = itertools.count(1)

    async def sink(message: str) -> None:
        await ctx.report_progress(progress=next(counter), total=None, message=message)

    return progress_sink(sink)


def create_mcp_server(*, db_path: str | None = None) -> Any:
    """Create and configure the FastMCP server with all fusion tools."""
    from fastmcp import Context, FastMCP
    from fastmcp.tools import ToolResult
    from mcp.types import ToolAnnotations

    tools = FusionTools(db_path=db_path)

    @asynccontextmanager
    async def lifespan(_server: Any) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await tools.aclose()

    mcp = FastMCP(name="fusion-code-orchestrator", instructions=INSTRUCTIONS, lifespan=lifespan)

    def panel_tool(title: str) -> ToolAnnotations:
        # Fusion changes nothing on the user's machine (readOnly), calls provider APIs (open
        # world) and may answer differently each time (not idempotent).
        return ToolAnnotations(
            title=title,
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        )

    run_schema = FusionToolResult.model_json_schema()

    @mcp.tool(
        description=ASK_DESCRIPTION,
        annotations=panel_tool("Ask the Fusion panel"),
        output_schema=run_schema,
    )
    async def fusion_ask(input: FusionAskInput, ctx: Context) -> ToolResult:
        with _progress_to(ctx):
            output = await tools.fusion_ask(input)
        return to_tool_result(present_run(output, input.detail))

    @mcp.tool(
        description=REVIEW_DESCRIPTION,
        annotations=panel_tool("Review a diff"),
        output_schema=run_schema,
    )
    async def fusion_review_diff(input: ReviewDiffInput, ctx: Context) -> ToolResult:
        with _progress_to(ctx):
            output = await tools.fusion_review_diff(input)
        return to_tool_result(present_run(output, input.detail))

    @mcp.tool(
        description=DEBUG_DESCRIPTION,
        annotations=panel_tool("Debug an error"),
        output_schema=run_schema,
    )
    async def fusion_debug_error(input: DebugErrorInput, ctx: Context) -> ToolResult:
        with _progress_to(ctx):
            output = await tools.fusion_debug_error(input)
        return to_tool_result(present_run(output, input.detail))

    @mcp.tool(
        description=ARCHITECTURE_DESCRIPTION,
        annotations=panel_tool("Decide an architecture question"),
        output_schema=run_schema,
    )
    async def fusion_decide_architecture(
        input: DecideArchitectureInput, ctx: Context
    ) -> ToolResult:
        with _progress_to(ctx):
            output = await tools.fusion_decide_architecture(input)
        return to_tool_result(present_run(output, input.detail))

    @mcp.tool(
        description=PLAN_DESCRIPTION,
        annotations=panel_tool("Plan a feature"),
        output_schema=run_schema,
    )
    async def fusion_plan_feature(input: PlanFeatureInput, ctx: Context) -> ToolResult:
        with _progress_to(ctx):
            output = await tools.fusion_plan_feature(input)
        return to_tool_result(present_run(output, input.detail))

    @mcp.tool(
        description=EVAL_DESCRIPTION,
        annotations=panel_tool("Evaluate an answer"),
        output_schema=run_schema,
    )
    async def fusion_eval_answer(input: EvalAnswerInput, ctx: Context) -> ToolResult:
        with _progress_to(ctx):
            output = await tools.fusion_eval_answer(input)
        return to_tool_result(present_run(output, input.detail))

    @mcp.tool(
        description=STATS_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Fusion usage and savings",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        output_schema=StatsToolResult.model_json_schema(),
    )
    async def fusion_stats(input: FusionStatsInput) -> ToolResult:
        return to_tool_result(present_stats(await tools.fusion_stats(input)))

    @mcp.tool(
        description=COMPARE_DESCRIPTION,
        annotations=panel_tool("Compare Claude Code runs"),
        output_schema=CompareToolResult.model_json_schema(),
    )
    async def fusion_compare_claude_runs(input: CompareClaudeRunsInput, ctx: Context) -> ToolResult:
        with _progress_to(ctx):
            output = await tools.fusion_compare_claude_runs(input)
        return to_tool_result(present_comparison(output))

    # -- resources: what a response leaves out, readable on demand -------------------------------

    @mcp.resource(
        "fusion://runs/{run_id}",
        name="run",
        description="One stored Fusion run: full answer, claims, per-call cost and warnings. "
        "The run_id comes from any tool response.",
        mime_type="application/json",
    )
    async def run_resource(run_id: str) -> str:
        record = await asyncio.to_thread(tools.run_record, run_id)
        cleaned, _ = harden(json.loads(json.dumps(record, default=str)))
        return json.dumps(cleaned, indent=2)

    @mcp.resource(
        "fusion://stats",
        name="stats",
        description="Cumulative Fusion spend, savings and shadow A/B results as Markdown.",
        mime_type="text/markdown",
    )
    async def stats_resource() -> str:
        output = await tools.fusion_stats(FusionStatsInput())
        return str(output["display_markdown"])

    @mcp.resource(
        "fusion://strategies",
        name="strategies",
        description="The strategies a tool call may name, and which one each budget preset means.",
        mime_type="application/json",
    )
    def strategies_resource() -> str:
        return json.dumps(tools.strategies_overview(), indent=2)

    # -- prompts: ready-made requests that name the right tool ------------------------------------

    @mcp.prompt(
        name="review-this-diff",
        description="Have Fusion's panel review a diff before you commit it.",
    )
    def review_this_diff(diff: str = "", focus: str = "") -> str:
        source = (
            f"Review this diff:\n\n{diff}"
            if diff.strip()
            else "Get the diff to review with `git diff` (add `--staged` for staged changes)."
        )
        goal = f" Focus on: {focus}." if focus.strip() else ""
        return (
            f"{source}\n\nCall the `fusion_review_diff` tool with the diff in `diff`, the "
            f"relevant background in `context`{goal} Then check each finding against the code "
            "and tell me which ones you confirmed, which you rejected and why."
        )

    @mcp.prompt(
        name="debug-this-error",
        description="Have Fusion's panel rank the likely causes of an error.",
    )
    def debug_this_error(error: str = "", context: str = "") -> str:
        source = (
            f"Debug this error:\n\n{error}"
            if error.strip()
            else "Take the most recent error from this session."
        )
        extra = f"\n\nBackground: {context}" if context.strip() else ""
        return (
            f"{source}{extra}\n\nCall the `fusion_debug_error` tool with the error message, the "
            "stack trace and the code involved. Then run the verification steps it suggests, "
            "starting with the likeliest cause, before changing any code."
        )

    @mcp.prompt(
        name="plan-this-feature",
        description="Have Fusion's panel draft an implementation plan for a feature.",
    )
    def plan_this_feature(feature: str = "", constraints: str = "") -> str:
        what = feature.strip() or "the feature we just discussed"
        limits = f" Constraints: {constraints}." if constraints.strip() else ""
        return (
            f"Plan this feature: {what}.{limits}\n\nCall the `fusion_plan_feature` tool with a "
            "description, the existing patterns it should follow and the relevant file excerpts. "
            "Then turn its plan into concrete steps for this repository and flag anything that "
            "does not fit the code."
        )

    return mcp


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run_server(
    *,
    db_path: str | None = None,
    transport: Transport = "stdio",
    host: str = DEFAULT_HTTP_HOST,
    port: int = DEFAULT_HTTP_PORT,
    allow_remote: bool = False,
) -> None:
    """Run the MCP server: over stdio (the default), or streamable HTTP on localhost.

    The HTTP server has no authentication and can spend the user's provider money, so it binds
    to a loopback address unless ``allow_remote`` says otherwise.
    """
    from fusion.config.env import load_env

    if transport == "http" and not _is_loopback(host) and not allow_remote:
        msg = (
            f"Refusing to serve Fusion on {host}: the HTTP transport has no authentication and "
            "every call spends provider money. Use 127.0.0.1, or pass --allow-remote to accept "
            "the risk."
        )
        raise ValueError(msg)
    load_env()
    mcp = create_mcp_server(db_path=db_path)
    if transport == "http":
        if not _is_loopback(host):
            print(
                f"fusion: serving MCP over HTTP on {host}:{port} without authentication",
                file=sys.stderr,
            )
        mcp.run(transport="http", host=host, port=port, show_banner=False)
        return
    # stdout is reserved for JSON-RPC; never print banners or logs there.
    mcp.run(show_banner=False)
