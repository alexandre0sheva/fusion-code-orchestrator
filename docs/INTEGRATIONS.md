# Integrations

Canonical setup guide for using Fusion from coding agents, plus the MCP tool reference. Fusion is an
MCP server, started by the client over stdio (or served on localhost over HTTP, see
[Transports](#transports)); the client stays the executor and applies edits itself.

**Do not run `uv run fusion mcp` manually in a terminal.** It speaks JSON-RPC on stdin/stdout and is
meant to be spawned by the client. Pressing Enter there sends invalid input and produces
`Invalid JSON: EOF` errors. To check that providers work, run:

```bash
uv run python evals/runners/compare_pipelines.py
```

## Claude Code

Requirements: [uv](https://docs.astral.sh/uv/) and Claude Code. Fusion runs straight from GitHub
with `uvx`: there is nothing to clone or publish. It needs Python 3.12 or newer, and `uvx` fetches
one (`--managed-python`) when the machine's default is older. Install it with one command, then give it keys.

```bash
uvx --python '>=3.12' --from git+https://github.com/alexandre0sheva/fusion-code-orchestrator fusion install claude-code --plugin
```

Or, from a checkout: `uv run fusion install claude-code --plugin`. Restart Claude Code, then check
with `/mcp` (the server `plugin:fusion:fusion` should be connected) and `/fusion:stats`.

**Provider keys** reach the server through the environment Claude Code starts in, or a `.env` file
in the project directory you open Claude Code in (the server starts there). Set at least one of
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`. Without any, Fusion answers with the
failures listed in `warnings`. Run `claude` from a shell that has them exported.

### Choose how to install

| You want | Run | Gets you |
|----------|-----|----------|
| Everything (recommended) | `fusion install claude-code --plugin` | The server, the `/fusion:*` commands, the skills and the `fusion-advisor` subagent |
| Only the tools, for all your projects | `fusion install claude-code` | The MCP server in your user config |
| Only the tools, shared with the team | `fusion install claude-code --scope project` | A `fusion` entry merged into the project's `.mcp.json` |
| The same, from the Claude Code prompt | `/plugin marketplace add alexandre0sheva/fusion-code-orchestrator`, then `/plugin install fusion@fusion-code-orchestrator` | The plugin, with no install command |

`--scope user|project` also applies to `--plugin` (default `user`). Options for every form:

| Option | Meaning |
|--------|---------|
| `--dry-run` | Print what would be done and the commands it would run; change nothing |
| `--ref TAG` | Pin the server to a git tag, branch or commit (`git+URL@TAG`). Pin a release tag once 0.2.0 is tagged |
| `--local-checkout PATH` | Run the server from a clone: `uv run --directory PATH fusion mcp` |
| `--force` | Replace a different `fusion` server that is already registered |
| `--no-verify` | Skip the launch check |

The installer is safe to run again: it does nothing when `fusion` is already registered with the
same command, and refuses (without `--force`) to replace one that differs. Before registering
anything it starts the server the way Claude Code would and lists its tools, on the mock provider,
so the check needs no keys and costs nothing; a server that does not start is not registered.
`.mcp.json` is merged, never overwritten, and a malformed file is refused untouched. Everything
else goes through the `claude` command (`claude mcp add`, `claude plugin install`), so Claude Code's
own config file is never edited behind its back. A PyPI release (`uvx fusion-code-orchestrator`)
would be a shorter command; it is not published, so none is documented.

By hand, the equivalent of the server-only install is:

```bash
claude mcp add --scope user fusion -- uvx --python '>=3.12' --managed-python --from git+https://github.com/alexandre0sheva/fusion-code-orchestrator fusion mcp
```

and, from a local clone, `claude mcp add --scope user fusion -- uv run --directory /path/to/fusion-code-orchestrator fusion mcp`.

### What the plugin adds

| | Name | Use |
|--|------|-----|
| Commands (you type them) | `/fusion:ask`, `/fusion:review`, `/fusion:debug`, `/fusion:plan`, `/fusion:decide`, `/fusion:eval` | One per tool; they gather the input (the diff, the error) and tell Claude how to read the answer |
| | `/fusion:stats` | Spend, savings and shadow A/B win-rate |
| | `/fusion:bench` | Summarise the latest benchmark run from `bench-results/` (read-only; never starts a study) |
| | `/fusion:ab` | Run a task with and without Fusion and compare the two |
| Skills (Claude loads them) | `fusion-orchestrator`, `fusion-review`, `fusion-debug`, `fusion-decide`, `fusion-plan`, `fusion-eval` | Each says when to call Fusion and when not to, so Claude skips it for trivial edits |
| Subagent | `fusion-advisor` | Calls Fusion in its own context and returns a verdict of at most 15 lines, with what it checked against the code. It can read files and call Fusion; it cannot edit or run commands |

Example prompts: "Use Fusion to review this diff before I merge", "Have the fusion-advisor look at
this stack trace", "Run `fusion_decide_architecture` for Redis vs Postgres caching".

Treat Fusion output like another model's answer: apply changes and run tests yourself. For
measuring whether Fusion helps, see [BENCHMARKING.md](BENCHMARKING.md).

### Smoke test

After installing, in a project directory with keys set:

1. `/mcp` lists `plugin:fusion:fusion` (or `fusion` after a server-only install) as connected.
2. `/fusion:stats` returns a summary without calling a model.
3. `/fusion:ask How should I retry a failed HTTP call?` shows progress (`panel 1/2 done` ...) and
   returns a short answer with a cost line.
4. Make a small change, then `/fusion:review`: it reads the diff and reports findings.
5. Ask "use the fusion-advisor to review my last change": the reply is a short verdict.
6. `fusion install claude-code --plugin` again reports "nothing to do".

### Troubleshooting

- **The server does not connect.** The first start downloads and builds Fusion (a clone and a
  build, up to a minute). Run `/mcp reconnect all`, or raise Claude Code's startup timeout:
  `MCP_TIMEOUT=120000 claude`. `uvx` must be on the `PATH` Claude Code sees.
- **Two `fusion` servers.** A server-only install and the plugin both define one. Keep the plugin
  and run `claude mcp remove fusion`.
- **A call is moved to the background.** Claude Code backgrounds MCP calls that run longer than two
  minutes (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`); Fusion's own soft limit returns a digest after 90
  seconds by default ([below](#progress-cancellation-and-the-soft-time-limit)).
- **Uninstall.** `claude plugin uninstall fusion@fusion-code-orchestrator`, and `claude mcp remove
  fusion` for a server-only install.

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

### Tools

Every tool is annotated `readOnlyHint: true` and `destructiveHint: false` (Fusion changes nothing
on your machine; it writes only its own run database), `openWorldHint: true` (it calls provider
APIs; `fusion_stats` calls none) and `idempotentHint: false`. Each has a typed `outputSchema`. The
descriptions the client shows the model say when to call the tool, when not to, and that a call
takes tens of seconds and costs cents.

| Tool | Call it for | Do not call it for |
|------|-------------|--------------------|
| `fusion_ask` | A second opinion on a hard, self-contained coding question | Trivial edits; anything the code in front of you answers |
| `fusion_review_diff` | A non-trivial or risky diff before commit or merge | Typo, formatting or one-line changes |
| `fusion_debug_error` | Ranked root causes and verification steps for an error that is not obvious | An error whose message names the cause |
| `fusion_decide_architecture` | A hard-to-reverse design choice | Choices that are easy to reverse or settled by convention |
| `fusion_plan_feature` | A plan for a multi-file feature whose approach is unclear | Small changes |
| `fusion_eval_answer` | Scoring a draft answer against a rubric | Finding out whether code works or a fact is true |
| `fusion_compare_claude_runs` | Measuring Claude Code + Opus against Claude Code + Fusion | Normal work |
| `fusion_stats` | Spend, savings and shadow A/B win-rate (free, calls no model) | |

### Inputs

The orchestration tools share these inputs (each tool adds its own, such as `diff` or
`error_message`):

| Input | Meaning |
|-------|---------|
| `context` | The one place for background text. `repo_context` and `repo_summary` (review) and `code_context` (debug) are still accepted, folded into `context`, and no longer in the schema |
| `file_snippets` | Short code excerpts, each prefixed with its path |
| `strategy` | A strategy name (`solo-cheap`, `panel-duo`, `panel-cheap`, `panel-refine`, `panel-cascade`, `panel-vote`, `panel-digest`); overrides `budget`. The list is the `fusion://strategies` resource; see [CONFIGURATION.md](CONFIGURATION.md#strategies-and-budgets) |
| `budget` | The older preset (`low`, `medium`, `high`, `local_only`); selects a strategy |
| `max_cost_usd` | A hard cap for this call, lowering (never raising) the strategy's own; the run is shifted to a cheaper form or refused before any model is called, see [CONFIGURATION.md](CONFIGURATION.md#cost-and-latency-caps) |
| `detail` | `compact` (default) or `full`, below |
| `shadow_baseline` | Force or suppress a shadow A/B run for this call |

With `panel-digest` no model merges the answers: the response lists the points several models
share, the disputed ones and the single-model ones, then each model's answer, and the calling agent
is the aggregator. The tool descriptions say so, so the agent keeps what the models agree on and
checks the rest against the code before relying on it.

### Responses

The text content is `display_markdown`; the same record is returned as structured content.

| Field | Compact (default) | Meaning |
|-------|:-----------------:|---------|
| `display_markdown` | yes | The answer, the top five claims, confidence and one cost line, cut at about 1,500 tokens (the cut says where the rest is) |
| `run_id`, `details_uri` | yes | Handle for the stored run: `fusion://runs/{run_id}` |
| `strategy`, `confidence`, `cost_usd`, `latency_s`, `models_called` | yes | Headline numbers; `cost_usd` is absent when a price is unknown |
| `warnings` | yes | Timeouts, provider failures, cost-cap shifts, soft-limit notices |
| `partial`, `halted` | yes | `partial`: the soft time limit cut the run short and the answer is the panel digest. `halted`: why there is no answer (`insufficient_context`, `quorum`, `budget`, `timeout`) |
| `result` | `full` only | Task-specific structured result |
| `claims`, `agreement` | `full` only | The panel's claims grouped across models; agreement score, evidence rate, calibrated `confidence` |
| `usage`, `cost_comparison`, `routing`, `evals` | `full` only | Per-model tokens, cost and latency; Fusion versus the baseline ([COSTS.md](COSTS.md)); why these models; scoring of each stage |
| `raw_outputs` | `full` only, with `include_raw_outputs` | Each panel answer |

`fusion_stats` returns `display_markdown`, `warnings` and `result`; `fusion_compare_claude_runs`
adds `evals`.

### Progress, cancellation and the soft time limit

- **Progress.** A client that sends a progress token receives a notification per step: `routing`,
  `panel: asking 3 models`, `panel 1/3 done` ... `panel 3/3 done`, `refining the panel's answers`,
  `synthesizing`, `checking the final answer`. A cascade also reports `escalation`. Progress counts
  up and has no total.
- **Cancellation.** When the client cancels a request, every provider call still in flight is
  cancelled with it (nothing runs on after the host gave up). Calls cancelled mid-flight are on the
  run's ledger with unknown cost, since the provider may have billed them.
- **Soft time limit.** If a call is still running after `FUSION_TOOL_SOFT_TIMEOUT_S` seconds
  (default 90; `0` or `off` disables), it stops waiting for models and returns the best result it
  has, with a warning and `partial: true`: the panel's digest (the answers and what they share and
  dispute, no synthesis) when the panel had answered, or an explanation and `halted: timeout` when
  no answer was ready. The limit does not apply to benchmark runs, and a partial answer is never
  cached. The limit is measured from the start of the panel, so a slow run is cut when it reaches
  the limit, not forecast to.

### Resources and prompts

| Resource | Contents |
|----------|----------|
| `fusion://runs/{run_id}` | One stored run: the full answer, claims, per-call cost and warnings (not the text you sent) |
| `fusion://stats` | The `fusion_stats` summary |
| `fusion://strategies` | The strategies a call may name and what each budget preset means |

| Prompt | Starts |
|--------|--------|
| `review-this-diff` (`diff`, `focus`) | A review with `fusion_review_diff`, then checking each finding against the code |
| `debug-this-error` (`error`, `context`) | A diagnosis with `fusion_debug_error`, then running its verification steps |
| `plan-this-feature` (`feature`, `constraints`) | A plan with `fusion_plan_feature`, then fitting it to the repository |

### Transports

`fusion mcp` speaks stdio, which the client spawns (the default). `fusion mcp --transport http
[--host 127.0.0.1] [--port 8765]` serves streamable HTTP at `/mcp` for clients that cannot spawn a
process. It has no authentication and every call spends provider money, so it binds only to a
loopback address; another `--host` is refused unless you pass `--allow-remote`.

### Clients that implement less

Fusion works without any of the optional parts. A client that ignores progress sees a normal
blocking call, one without resources gets the whole answer with `detail: full`, and one without
prompts just uses the tools. Which client supports what is recorded with the per-client setup
above as each is verified.
