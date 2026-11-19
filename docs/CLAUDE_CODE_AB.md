# Claude Code A/B: with and without Fusion

Canonical for the Claude Code A/B procedure: the automated end-to-end harness (`fusion bench e2e`)
and the manual comparison on your own tasks. Benchmark methodology in general (datasets, scoring,
statistics, reports) is in [BENCHMARKING.md](BENCHMARKING.md).

The question is the one that matters for cost: inside a real Claude Code session, with the host
model's own tokens counted, does a cheaper executor that consults Fusion solve as many tasks as a
frontier executor, for less? Fusion never edits files: Claude Code stays the executor in every arm.

## Automated: `fusion bench e2e`

Each run is headless Claude Code (`claude -p`) on one executable coding task of a dataset (default
`v2`: every `coding` task with hidden tests, synthetic and real-repo), in a sandboxed temporary copy
of the task's repo. The hidden tests decide whether it solved the task.

| Arm | Executor | Fusion |
|-----|----------|--------|
| `opus`, `sonnet`, `haiku` | Claude Code on Opus 5.5, Sonnet 5.5 or Haiku 5.5 (the catalog's model ids) | off: no MCP server |
| `opus+fusion`, `sonnet+fusion`, `haiku+fusion` | the same | on: Fusion's MCP server and plugin |

The `haiku+fusion` and `sonnet+fusion` arms (a cheap executor with a cheap advisor) are the cost
play; `opus` is the baseline.

### Commands

```bash
# Free and deterministic: a simulated `claude` replays seeded runs (no API keys, no network).
uv run fusion bench e2e --mock --limit 4

# Live, within a cap you state (costs money: host tokens plus Fusion's panels).
uv run fusion bench e2e --limit 6 --executors opus,haiku --max-usd 3

# Continue a stopped run (finished runs are skipped, errors are tried again).
uv run fusion bench e2e --run-id bench-20261119-101500-ab12 --max-usd 4

# Read the results: the usual reports, with a "Claude Code end to end" section.
uv run fusion bench show RUN_ID
uv run fusion bench report RUN_ID --format html
```

| Option | Default | Meaning |
|--------|---------|---------|
| `--dataset` | `v2` | Any dataset; only its executable `coding` tasks are used |
| `--split`, `--limit`, `--seed` | `dev`, all, `0` | Which tasks (a seeded sample spread evenly) |
| `--executors` | `opus,sonnet,haiku` | Executors to run |
| `--fusion` | `both` | `both`, `off` or `on` |
| `--repeats` | `1` | Runs of each (task, arm) |
| `--max-usd` | required live | The run's cap |
| `--run-budget-usd` | `1.0` | Claude Code's own `--max-budget-usd` for each run |
| `--fusion-allowance-usd` | `0.25` | Reserved for Fusion's calls in each Fusion run |
| `--spend-stop-usd` | the ledger's cap | Stop before cumulative live spend would pass this |
| `--timeout` | `900` | Seconds before a run is killed |
| `--concurrency` | `1` | Runs at once |
| `--plugin-dir` | this checkout's `plugin/` | The plugin the Fusion arms load |
| `--claude` | `claude` | The executable |

### Before a live run

- Claude Code on `PATH` (`claude --version`; the version is stored with the run), new enough to
  know every executor's model id. A build that predates a model prints `unrecognized_model`, prices
  it at a default rate and so stops early on its own `--max-budget-usd`; the item then carries a
  warning. Claude Code 2.1.287 knows Opus 5.5 and Sonnet 5.5 but not Haiku 5.5; 2.1.327 knows all three
  (`claude update`).
- `ANTHROPIC_API_KEY`: runs use `--bare`, which reads no login, keychain, hooks, `CLAUDE.md` or
  installed plugins, so every arm starts from the same clean setup and the host cost is billed to
  that key.
- Provider keys for the Fusion server (`.env`, see [CONFIGURATION.md](CONFIGURATION.md)); they are
  handed to the server only, never to Claude Code's environment.
- A share of the live budget: `fusion bench spend` shows what is left. Each run reserves its worst
  case (`--run-budget-usd`, plus `--fusion-allowance-usd` for a Fusion arm) against `--max-usd` and
  the spend ledger before it starts, and the run stops cleanly, resumable, when the next one would
  not fit. Its measured cost (host plus Fusion) is appended to the ledger afterwards.

### What one run does

1. Copies the task's files into a fresh temporary directory (`bench/sandbox.py`) and commits them to
   a throwaway git repository there, so `git diff` and Fusion's `diff_ref` work. The task's source
   is only read.
2. Starts `claude -p "<task>" --output-format stream-json --model <id> --max-budget-usd <cap>
   --bare --permission-mode acceptEdits --permission-prompts none --settings <file>
   --strict-mcp-config --mcp-config <file>` in that copy, with `HOME` and `CLAUDE_CONFIG_DIR` inside
   it and a scrubbed environment. A Fusion arm also gets `--plugin-dir` (a copy of the plugin
   without its `.mcp.json`), `--allowedTools mcp__fusion`, and one appended system-prompt paragraph
   telling it which Fusion tools to use when. The task prompt itself is the same in every arm: the
   task's text, asking for edits in place.
3. In a Fusion arm, the MCP config starts this checkout's server (`python -m fusion.cli.main mcp`)
   with its project root on the copy and its run database inside the sandbox.
4. Reads the result: Claude Code's final `result` event (cost, usage, turns), every `tool_use` in
   the transcript, and the Fusion server's ledger (`runs.db`: one row per Fusion run, with its cost).
5. Diffs the edited copy against the task and runs the hidden tests on that diff in a second,
   network-less sandbox, exactly as a patch from any bench arm is judged
   ([Coding tasks and the sandbox](BENCHMARKING.md#coding-tasks-and-the-sandbox)). Then deletes the
   copy.

### Isolation

| Control | How |
|---------|-----|
| Task source | Copied, never written; a test hashes the dataset before and after a run |
| Bash commands | Claude Code's own sandbox (`sandbox.enabled`, `failIfUnavailable`, no unsandboxed retry), with an empty strict network allow-list: the task's code gets no network |
| Web tools | `WebFetch` and `WebSearch` denied |
| File edits | `acceptEdits` covers the working directory only; anything else needs a permission prompt, and `--permission-prompts none` answers every prompt "no" |
| Secrets | The run's MCP config (with the provider keys) sits beside the copy, in a directory Claude Code's file tools and sandbox are denied |
| Limits | A wall-clock timeout that kills the process group, Claude Code's `--max-budget-usd` |

The `claude` process itself is not under Fusion's `sandbox-exec`: Claude Code needs the network for
its own API calls, and on macOS its Bash sandbox cannot start inside another Seatbelt profile. The
guarantees above therefore rest on Claude Code's sandbox and permission settings. See
[SECURITY.md](../SECURITY.md#benchmark-sandbox).

### What is measured

Stored with every item (`BenchItem.e2e`; the bench's own `metrics` hold the same in its terms):

| Field | Source |
|-------|--------|
| `tests_passed` / `tests_expected`, `solved` | The task's hidden tests on the edited copy |
| `wall_seconds` | Wall time of the `claude` process (`claude_seconds`: Claude Code's own figure) |
| `host_cost_usd` | Claude Code's per-model token counts (`modelUsage` in its JSON) at the catalog's prices; a model the catalog lacks counts at Claude Code's own `costUSD` (the item says which). Unknown when neither applies, and the report marks such totals as a lower bound |
| `host_cost_reported_usd`, `host_usage` | Claude Code's own `total_cost_usd` and `modelUsage`, kept for comparison: for a model it does not know, its cost uses a default price |
| `fusion_cost_usd`, `fusion_calls` | The Fusion server's ledger in the sandbox |
| `total_cost_usd` | Host plus Fusion |
| `turns`, `tool_calls`, `fusion_tool_calls` | `num_turns`; `tool_use` blocks in the transcript |
| `input_tokens`, `output_tokens`, `cache_read_tokens` | Claude Code's `usage` (input counts cache reads and writes) |
| `files_changed`, `patch` | The diff of the edited copy |

Claude Code's raw output of each run is kept beside the results, in
`bench-results/<run>/evidence/e2e/<job key>.jsonl` (and `.stderr.txt`).

An item is `completed` when Claude Code finished, `halted` when it stopped on its own limit, its
budget or the timeout (the tests still judge what it left), and `error` when it produced no result
or a Fusion arm's server did not connect (such a run is not a Fusion run; `--run-id` tries errors
again).

### Simulated runs (`--mock`)

`--mock` replaces `claude` with `python -m fusion.bench.e2e.fake_claude`, which replays a seeded
recording per (task, arm): the executor's invented success rate decides whether it writes the task's
reference fix, one of its known flaws, or nothing; tokens are priced from the catalog; Fusion arms
record Fusion runs in the sandboxed ledger. Everything else (the copy, the hidden tests, the store,
the reports) is the real code path. The numbers test the harness and say nothing about real models.

## Manual: your own tasks

To compare on a task from your own repository instead of a dataset:

1. Start from the same clean repository state for both arms, with the same `task.md` (the exact
   prompt and acceptance criteria).
2. **Arm A:** Claude Code on Opus, told not to call Fusion. **Arm B:** Claude Code on a cheaper
   model with Fusion installed ([INTEGRATIONS.md](INTEGRATIONS.md)), told to call `fusion_ask` or
   `fusion_plan` before editing, `fusion_debug_error` after a failing test, and
   `fusion_review_diff` before finishing. Let both edit, run tests and iterate normally;
   `/fusion:ab` does this inside one session.
3. Record for each arm: the final answer, the tests run and their results, the whole wall-clock
   time, and the cost (Claude Code's usage; Fusion's from `fusion runs show RUN_ID`). Leave a cost
   unknown rather than guess it.
4. Compare with the MCP tool `fusion_compare_claude_runs`, or from files:

   ```bash
   uv run fusion compare-claude-runs \
     --task-file task.md --opus-file opus-output.md --fusion-file fusion-output.md \
     --context-file verification.md \
     --opus-cost 0.42 --fusion-cost 0.07 --opus-latency-ms 90000 --fusion-latency-ms 45000
   ```

   It returns a judged quality winner and the cheaper and faster arm (`unknown` where a value is
   missing). Prefer passing tests over a judge's opinion, and do not count a cheaper arm as better
   when its tests fail. One task proves little.
