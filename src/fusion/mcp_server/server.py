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
import warnings
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from urllib.request import url2pathname

from fusion.mcp_server.patch import present_patch, present_selection
from fusion.mcp_server.response import (
    DIGEST_OVERHEAD_TOKENS,
    present_comparison,
    present_feedback,
    present_run,
    present_stats,
    to_tool_result,
)
from fusion.mcp_server.schemas import (
    CompareClaudeRunsInput,
    CompareToolResult,
    DebugErrorInput,
    DecideArchitectureInput,
    DigestInput,
    EvalAnswerInput,
    FeedbackInput,
    FeedbackResult,
    FusionAskInput,
    FusionStatsInput,
    FusionToolResult,
    PatchToolResult,
    PlanFeatureInput,
    PlanInput,
    ProposePatchInput,
    ReviewDiffInput,
    SelectPatchInput,
    SelectPatchResult,
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
    "yourself. Use it for a second opinion on review, debugging, design, planning and hard coding "
    "questions, not for trivial edits. A call takes tens of seconds and costs cents. Name files in "
    "`paths`/`globs` and a git range in `diff_ref` instead of pasting them: Fusion reads them "
    "itself, read-only, and fusion_digest answers questions about large material in a few "
    "hundred tokens. fusion_propose_patch returns a unified diff (never applied: you apply it) "
    "chosen by running the panel's checks and your tests in a sandbox copy. Responses are "
    "compact (answer, top claims, confidence, cost); detail='full' or fusion://runs/{run_id} "
    "has the rest. max_cost_usd caps spend, strategy picks a cheaper or faster form "
    "(fusion://strategies); with panel-digest you merge the panel's answers yourself. A call "
    "past the soft time limit returns the panel's digest and says so."
)

_TEXT_ONLY = (
    " Fusion returns text and runs nothing: check its claims against the code before editing. "
    "A call takes tens of seconds and costs cents; the response states the cost."
)
_READS = " Name files in `paths` or `globs` instead of pasting them: Fusion reads them itself."
_VERIFY = _TEXT_ONLY

ASK_DESCRIPTION = (
    "Ask a panel of cheap models a self-contained coding question and get one answer with the "
    "points the models agree on. Call it for a second opinion on a hard implementation question "
    "when a wrong answer would be costly. Do not call it for trivial edits or questions the code "
    "in front of you answers." + _READS + _VERIFY
)
REVIEW_DESCRIPTION = (
    "Review a code diff with a panel of cheap models; findings are grouped by how many models "
    "agree. Call it before committing or merging a risky change (security, concurrency, "
    "migrations, public APIs) or to check your own patch. Do not call it for typo, formatting or "
    "one-line changes. Pass `diff_ref` (such as 'HEAD~1' or 'main...HEAD'): Fusion reads the "
    "diff itself (send `diff` text only for a change git cannot show) and name related files in "
    "`paths`." + _VERIFY
)
DEBUG_DESCRIPTION = (
    "Rank the likely root causes of an error, with steps to verify each and a minimal fix. Call "
    "it when the cause is not obvious after a first look, or after a fix failed. Do not call it "
    "when the message already names the cause. Name the files involved in `paths`; pass "
    "`diff_ref` when a recent change may be the cause." + _VERIFY
)
PLAN_DESCRIPTION = (
    "Plan with a panel of cheap models. kind='feature' (default): an implementation plan with "
    "steps, affected modules, tests, risks and open questions. kind='decision': compare design "
    "options and get a recommendation with tradeoffs, reversibility and a migration plan. Call "
    "it at the start of a multi-file feature, or before a choice that is hard to reverse "
    "(storage, queues, service boundaries). Do not call it for small changes you can already "
    "sketch or choices that project conventions settle." + _READS + _VERIFY
)
EVAL_DESCRIPTION = (
    "Score an answer against its question and a rubric: strengths, weaknesses, unsupported "
    "claims, missing points. Call it to check a draft answer before you act on or present it. "
    "Do not call it to find out whether code works or a fact is true: it judges only the text "
    "you pass in and runs nothing." + _TEXT_ONLY
)
DIGEST_DESCRIPTION = (
    "Have cheap models read large material (`paths`/`globs`, or a `diff_ref` diff) and return a "
    "short answer to one question, at most `max_answer_tokens`. Call it when answering would "
    "mean reading many or long files into your own context. Do not call it for a file you need "
    "to edit or for one short file." + _TEXT_ONLY
)
PATCH_DESCRIPTION = (
    "Get a patch for a change from a panel of cheap models: each writes a patch and checks, "
    "every patch is run against everyone's checks (and your `tests_cmd`) in a sandbox copy of "
    "the project, and the one passing the most checks is returned as a unified diff. Fusion "
    "never applies it or changes your files: you apply it. Call it for a bug fix or small change "
    "where a wrong patch is costly and checks can tell right from wrong. Do not call it for large "
    "refactors or edits you can make directly. Name the files in `paths`. verify='host' runs "
    "nothing: you get two candidates and their checks, then call fusion_select_patch. A call "
    "takes tens of seconds to a few minutes and costs cents."
)
SELECT_PATCH_DESCRIPTION = (
    "After fusion_propose_patch with verify='host': pass its run_id and the results of running "
    "its checks on each candidate (and on the code as it is) to get the best patch. Free: no "
    "model is called and nothing runs. Only for that second step."
)
FEEDBACK_DESCRIPTION = (
    "Tell Fusion what became of one of its answers (used, partly, wrong, ignored) with a "
    "one-line note. With library.enabled one cheap call distils at most one per-project lesson "
    "that later panel prompts carry; otherwise nothing is stored and no model is called. Call it "
    "after you acted on, or rejected, a Fusion answer, especially a wrong one. Do not call it for "
    "runs you have not checked. The note is redacted and stays in Fusion's local database."
)
STATS_DESCRIPTION = (
    "Cumulative Fusion usage: spend against the baseline model's estimated cost, savings, host "
    "tokens saved, call counts, shadow A/B win-rate. Free: it reads the local run database and "
    "calls no model. Call it when asked whether Fusion is paying off."
)
COMPARE_DESCRIPTION = (
    "Score two finished Claude Code outputs for the same task (one made with Opus alone, one "
    "with Fusion) against one rubric and report which is better, cheaper and faster. Only for "
    "measuring Fusion against a baseline when the user asks; it runs two evaluation panels, so "
    "it costs more than other tools."
)


ALIAS_TAG = "alias"


def deprecated(output: dict[str, Any], old: str, new: str, how: str) -> dict[str, Any]:
    """The response of a 0.2.0 tool name that is no longer listed: its answer plus what to call."""
    note = f"`{old}` is deprecated and will be removed after 0.3.0: call `{new}` with {how}."
    output["warnings"] = [*output.get("warnings", []), note]
    output["display_markdown"] = f"{output.get('display_markdown', '')}\n\n_{note}_"
    return output


def _unlisted_aliases() -> Any:
    from fastmcp.server.middleware import Middleware

    class UnlistedAliases(Middleware):
        """Hide the alias tools from ``tools/list``; ``tools/call`` still reaches them."""

        async def on_list_tools(self, context: Any, call_next: Any) -> Any:
            tools = await call_next(context)
            return [t for t in tools if ALIAS_TAG not in (t.tags or set())]

    return UnlistedAliases()


def _progress_to(ctx: Any) -> AbstractContextManager[None]:
    """Forward the run's progress messages to the client (a no-op without a progress token)."""
    counter = itertools.count(1)

    async def sink(message: str) -> None:
        await ctx.report_progress(progress=next(counter), total=None, message=message)

    return progress_sink(sink)


ROOTS_TIMEOUT_S = 5.0


async def client_root(ctx: Any) -> Path | None:
    """The first filesystem root the MCP client declares, or None (then ``FUSION_READ_ROOT``
    or the server's cwd is the project root). None too for clients without the roots
    capability, transports with no channel for server-to-client requests, and clients that
    do not answer within ``ROOTS_TIMEOUT_S``."""
    from mcp.shared.exceptions import MCPDeprecationWarning
    from mcp.types import ClientCapabilities, RootsCapability

    session = getattr(ctx, "session", None)
    if session is None or not getattr(session, "can_send_request", True):
        return None  # MCP 2026-07-28 transports may have no channel for server requests
    try:
        if not session.check_client_capability(ClientCapabilities(roots=RootsCapability())):
            return None
        with warnings.catch_warnings():  # roots are deprecated from MCP 2026-07-28 (SEP-2577)
            warnings.simplefilter("ignore", MCPDeprecationWarning)
            async with asyncio.timeout(ROOTS_TIMEOUT_S):
                listed = await session.list_roots()
    except Exception as exc:  # no back channel, a timeout, a client error: fall back to cwd
        print(f"fusion: could not list the client's roots ({exc}); using cwd", file=sys.stderr)
        return None
    for root in listed.roots:
        uri = str(root.uri)
        if uri.startswith("file://"):
            return Path(url2pathname(urlparse(uri).path))
    return None


async def _root_for(input: Any, ctx: Any) -> Path | None:
    return await client_root(ctx) if getattr(input, "reads_workspace", False) else None


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
    mcp.add_middleware(_unlisted_aliases())

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
            output = await tools.fusion_ask(input, root=await _root_for(input, ctx))
        return to_tool_result(present_run(output, input.detail))

    @mcp.tool(
        description=REVIEW_DESCRIPTION,
        annotations=panel_tool("Review a diff"),
        output_schema=run_schema,
    )
    async def fusion_review_diff(input: ReviewDiffInput, ctx: Context) -> ToolResult:
        with _progress_to(ctx):
            output = await tools.fusion_review_diff(input, root=await _root_for(input, ctx))
        return to_tool_result(present_run(output, input.detail))

    @mcp.tool(
        description=DEBUG_DESCRIPTION,
        annotations=panel_tool("Debug an error"),
        output_schema=run_schema,
    )
    async def fusion_debug_error(input: DebugErrorInput, ctx: Context) -> ToolResult:
        with _progress_to(ctx):
            output = await tools.fusion_debug_error(input, root=await _root_for(input, ctx))
        return to_tool_result(present_run(output, input.detail))

    @mcp.tool(
        description=PLAN_DESCRIPTION,
        annotations=panel_tool("Plan a feature or decide a design"),
        output_schema=run_schema,
    )
    async def fusion_plan(input: PlanInput, ctx: Context) -> ToolResult:
        with _progress_to(ctx):
            output = await tools.fusion_plan(input, root=await _root_for(input, ctx))
        return to_tool_result(present_run(output, input.detail))

    # The 0.2.0 names stay callable for one release but are not listed: a listed tool costs the
    # host its schema on every turn. Their answers say what to call instead.
    @mcp.tool(
        description="Deprecated: use fusion_plan with kind='decision'.",
        tags={ALIAS_TAG},
        annotations=panel_tool("Decide an architecture question"),
        output_schema=run_schema,
    )
    async def fusion_decide_architecture(
        input: DecideArchitectureInput, ctx: Context
    ) -> ToolResult:
        with _progress_to(ctx):
            root = await _root_for(input, ctx)
            output = await tools.fusion_decide_architecture(input, root=root)
        output = deprecated(output, "fusion_decide_architecture", "fusion_plan", "kind='decision'")
        return to_tool_result(present_run(output, input.detail))

    @mcp.tool(
        description="Deprecated: use fusion_plan with kind='feature'.",
        tags={ALIAS_TAG},
        annotations=panel_tool("Plan a feature"),
        output_schema=run_schema,
    )
    async def fusion_plan_feature(input: PlanFeatureInput, ctx: Context) -> ToolResult:
        with _progress_to(ctx):
            output = await tools.fusion_plan_feature(input, root=await _root_for(input, ctx))
        output = deprecated(output, "fusion_plan_feature", "fusion_plan", "kind='feature'")
        return to_tool_result(present_run(output, input.detail))

    @mcp.tool(
        description=DIGEST_DESCRIPTION,
        annotations=panel_tool("Digest project files"),
        output_schema=run_schema,
    )
    async def fusion_digest(input: DigestInput, ctx: Context) -> ToolResult:
        with _progress_to(ctx):
            output = await tools.fusion_digest(input, root=await _root_for(input, ctx))
        cap = input.max_answer_tokens + DIGEST_OVERHEAD_TOKENS
        return to_tool_result(present_run(output, input.detail, max_tokens=cap))

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
        description=PATCH_DESCRIPTION,
        # It runs checks only in a temporary copy (deleted afterwards): nothing of the user's
        # changes, so it is read-only for the client.
        annotations=panel_tool("Propose a verified patch"),
        output_schema=PatchToolResult.model_json_schema(),
    )
    async def fusion_propose_patch(input: ProposePatchInput, ctx: Context) -> ToolResult:
        with _progress_to(ctx):
            output = await tools.fusion_propose_patch(input, root=await client_root(ctx))
        return to_tool_result(present_patch(output))

    @mcp.tool(
        description=SELECT_PATCH_DESCRIPTION,
        annotations=ToolAnnotations(
            title="Pick a patch from your check results",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        output_schema=SelectPatchResult.model_json_schema(),
    )
    async def fusion_select_patch(input: SelectPatchInput) -> ToolResult:
        return to_tool_result(present_selection(await tools.fusion_select_patch(input)))

    @mcp.tool(
        description=FEEDBACK_DESCRIPTION,
        # It writes only to Fusion's own database, never the user's files.
        annotations=panel_tool("Give feedback on a Fusion answer"),
        output_schema=FeedbackResult.model_json_schema(),
    )
    async def fusion_feedback(input: FeedbackInput, ctx: Context) -> ToolResult:
        output = await tools.fusion_feedback(input, root=await client_root(ctx))
        return to_tool_result(present_feedback(output))

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
        goal = f" Focus on: {focus}." if focus.strip() else ""
        if diff.strip():
            how = (
                f"Review this diff:\n\n{diff}\n\nCall the `fusion_review_diff` tool with the "
                "diff in `diff`"
            )
        else:
            how = (
                "Review my latest changes. Call the `fusion_review_diff` tool with `diff_ref` "
                "set to the committed range to review (such as 'HEAD~1' or 'main...HEAD'); "
                "Fusion reads that `git diff` itself. For uncommitted work, get the text with "
                "`git diff` (add `--staged` for staged changes) and pass it in `diff`"
            )
        return (
            f"{how}, and the relevant background in `context`.{goal} Then check each finding "
            "against the code and tell me which ones you confirmed, which you rejected and why."
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
            f"Plan this feature: {what}.{limits}\n\nCall the `fusion_plan` tool with a "
            "description, the existing patterns it should follow and the relevant files in "
            "`paths`. "
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
