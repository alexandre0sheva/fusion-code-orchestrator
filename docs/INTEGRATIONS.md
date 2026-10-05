# Integrations

Canonical setup guide for using Fusion from coding agents, plus the MCP tool reference. Fusion is a
stdio MCP server started by the client; the client stays the executor and applies edits itself.

**Do not run `uv run fusion mcp` manually in a terminal.** It speaks JSON-RPC on stdin/stdout and is
meant to be spawned by the client. Pressing Enter there sends invalid input and produces
`Invalid JSON: EOF` errors. To check that providers work, run:

```bash
uv run python evals/runners/compare_pipelines.py
```

## Claude Code

1. Install the package and add your keys (see the [README quickstart](../README.md#quickstart)).
2. Add the server, using one of these options.

**Option A: local plugin directory (recommended).** In Claude Code settings, add a plugin source
pointing to `/path/to/fusion-code-orchestrator/plugin`. The manifest (`plugin/plugin.json`)
registers the MCP server and the skills in `plugin/skills/`.

**Option B: manual MCP config.**

```json
{
  "mcpServers": {
    "fusion": {
      "command": "uv",
      "args": ["run", "fusion", "mcp"],
      "cwd": "/absolute/path/to/fusion-code-orchestrator"
    }
  }
}
```

Set `cwd` to your clone so `uv` finds the project and loads `.env`.

3. Restart Claude Code to load the MCP server and skills.

Skills teach Claude when to call Fusion and when to handle trivial edits directly:

| Skill | When Claude should use it |
|-------|---------------------------|
| `fusion-review` | Complex or security-sensitive diffs |
| `fusion-debug` | Unclear root causes, production errors |
| `fusion-decide` | Architecture trade-offs |
| `fusion-plan` | Non-trivial feature implementation plans |
| `fusion-eval` | Scoring answers before acting on them |

Example prompts inside Claude Code:

- "Use Fusion to review this diff before I merge."
- "Call `fusion_debug_error` with this stack trace and logs."
- "Run `fusion_decide_architecture` for Redis vs Postgres caching."

Treat Fusion output like another model response: apply changes and run tests yourself. For measuring
whether Fusion helps, see [BENCHMARKING.md](BENCHMARKING.md).

## Cursor

Add to `.cursor/mcp.json` in this repo (or Cursor's MCP settings):

```json
{
  "mcpServers": {
    "fusion": {
      "command": "uv",
      "args": ["run", "fusion", "mcp"],
      "cwd": "/absolute/path/to/fusion-code-orchestrator"
    }
  }
}
```

## Codex

Not documented yet. One-command installers for Codex, Cursor and Claude Code are planned for 0.2.0
(see the [roadmap](superpowers/plans/2026-10-05-v0.2.0-roadmap.md)).

## MCP tool reference

| Tool | Description |
|------|-------------|
| `fusion_ask` | General model-like coding answer using the Fusion panel |
| `fusion_review_diff` | Multi-model code review with synthesis |
| `fusion_debug_error` | Debug analysis with fix recommendations |
| `fusion_decide_architecture` | Architecture decision support |
| `fusion_plan_feature` | Implementation planning |
| `fusion_eval_answer` | Answer quality evaluation |
| `fusion_compare_claude_runs` | Compare Claude Code + Opus vs Claude Code + Fusion outputs |
| `fusion_stats` | Cumulative spend vs baseline, savings and shadow A/B win-rate |

Orchestration tools accept an optional `strategy` (for example `solo-cheap`, `panel-cheap`,
`panel-refine`, `panel-digest`), or the older `budget` (`low`, `medium`, `high`, `local_only`), which
selects a strategy; see [CONFIGURATION.md](CONFIGURATION.md#strategies-and-budgets). They also take
`shadow_baseline: true|false` to force or suppress a shadow A/B run against the real baseline model
for that call.

Each tool returns top-level task-specific fields and a consistent envelope:

| Field | Meaning |
|-------|---------|
| `display_markdown` | Compact Claude Code-facing summary: recommendation, confidence, cost, usage, caveats |
| `result` | Structured task-specific result object |
| `evals` | Context, per-answer, disagreement, judge and final eval data |
| `usage` | Per-model token, cost, latency and failure telemetry |
| `cost_comparison` | Fusion vs configured baseline (see [COSTS.md](COSTS.md)) |
| `routing` | Selected panel, judge, synthesizer, risk, complexity and routing reasons |
| `warnings` | Timeouts, provider failures, config caveats and budget warnings |
| `run_id` | SQLite trace ID for `uv run fusion runs show RUN_ID` |
