"""Shape what a tool returns: a short text for the model, a typed record for programs.

The pipelines produce one large output per run. A coding agent pays for every token a tool
returns, so the default response keeps only what it needs to act (the answer, the top claims, the
confidence, one cost line) and leaves the rest in the run store, under ``run_id``, for the
``fusion://runs/{run_id}`` resource. ``detail: full`` returns everything in the response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fusion.mcp_server.schemas import (
    CompareToolResult,
    FusionToolResult,
    StatsToolResult,
    ToolEnvelope,
)
from fusion.routing.budget import CHARS_PER_TOKEN
from fusion.security.output import harden

if TYPE_CHECKING:
    from fastmcp.tools import ToolResult

# The compact display text stays under about this many tokens, whatever the answer's length.
COMPACT_MAX_TOKENS = 1500

_FULL_FIELDS = (
    "result",
    "claims",
    "agreement",
    "usage",
    "cost_comparison",
    "routing",
    "evals",
    "raw_outputs",
)


def details_uri(run_id: str) -> str:
    return f"fusion://runs/{run_id}"


def cap_markdown(text: str, run_id: str, *, max_tokens: int = COMPACT_MAX_TOKENS) -> str:
    """Cut an over-long compact text at a line boundary and say where the rest is."""
    limit = int(max_tokens * CHARS_PER_TOKEN)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind("\n")
    if boundary > limit // 2:
        cut = cut[:boundary]
    return (
        f"{cut.rstrip()}\n\n_(Shortened to fit {max_tokens} tokens. The complete answer is in "
        f"`{details_uri(run_id)}` or call again with `detail: full`.)_"
    )


def hardened(fields: dict[str, Any]) -> dict[str, Any]:
    """The response fields with terminal escapes and bidi overrides removed from every string and
    sizes capped (``security.output``). Model text is untrusted: it reaches Claude Code's context
    and, through the CLI, a terminal. Says so in ``warnings`` when it changed anything."""
    cleaned, report = harden(fields)
    notes: list[str] = []
    if report.removed_chars:
        notes.append(
            f"Removed {report.removed_chars:,} terminal control characters from model output."
        )
    if report.truncated:
        notes.append(
            "Output truncated to bound the response size; the complete run is stored "
            f"(see {cleaned.get('details_uri', 'fusion://runs/{run_id}')})."
        )
    if notes:
        cleaned["warnings"] = [*cleaned.get("warnings", []), *notes]
    return dict(cleaned)


def present_run(output: dict[str, Any], detail: str) -> FusionToolResult:
    """The tool response for one pipeline output (a ``model_dump`` of its output model)."""
    run_id = str(output["run_id"])
    cost = output.get("cost_latency") or {}
    usage = output.get("usage") or {}
    routing = output.get("routing") or {}
    markdown = str(output.get("display_markdown", ""))
    full = detail == "full"
    fields: dict[str, Any] = {
        "display_markdown": markdown if full else cap_markdown(markdown, run_id),
        "warnings": list(output.get("warnings", [])),
        "run_id": run_id,
        "strategy": routing.get("strategy"),
        "confidence": output.get("confidence"),
        "cost_usd": cost.get("total_cost_usd") if cost.get("total_cost_known", True) else None,
        "latency_s": round(float(cost.get("total_latency_ms", 0.0)) / 1000, 2),
        "models_called": int(usage.get("successful_model_calls", 0)),
        "partial": bool(output.get("partial", False)),
        "halted": output.get("halt_reason"),
        "details_uri": details_uri(run_id),
    }
    if full:
        fields.update({name: output.get(name) for name in _FULL_FIELDS})
    return FusionToolResult(**hardened(fields))


def present_stats(output: dict[str, Any]) -> StatsToolResult:
    return StatsToolResult(
        **hardened(
            {
                "display_markdown": str(output["display_markdown"]),
                "warnings": list(output.get("warnings", [])),
                "result": output["result"],
            }
        )
    )


def present_comparison(output: dict[str, Any]) -> CompareToolResult:
    return CompareToolResult(
        **hardened(
            {
                "display_markdown": str(output["display_markdown"]),
                "warnings": list(output.get("warnings", [])),
                "result": output["result"],
                "evals": output["evals"],
            }
        )
    )


def to_tool_result(result: ToolEnvelope) -> ToolResult:
    """Text content for the model and the same record, typed, as structured content."""
    from fastmcp.tools import ToolResult  # imported here: the CLI must not load FastMCP to start

    return ToolResult(
        content=result.display_markdown,
        structured_content=result.model_dump(exclude_none=True),
    )
