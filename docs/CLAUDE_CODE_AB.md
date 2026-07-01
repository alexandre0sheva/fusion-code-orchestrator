# Claude Code + Opus vs Claude Code + Fusion A/B Runbook

This runbook describes the intended production workflow:

- **Arm A:** Claude Code uses its native large model, for example Opus.
- **Arm B:** Claude Code keeps all normal Claude Code capabilities, but calls Fusion MCP as
  the reasoning/model panel using cheaper models.
- **Comparison:** Fusion evaluates both outputs and reports quality, cost, and latency
  differences.

## What Fusion can and cannot replace

Fusion MCP cannot invisibly change Claude Code's internal model selector. Claude Code still
owns repository reads, edits, shell commands, tests, tool calls, and final delivery.

Fusion acts as a model-like reasoning backend that Claude Code calls through MCP:

```text
Claude Code tools/editing/testing stay the same.
Only the expensive reasoning step is routed through Fusion's cheaper model panel.
```

For a fair A/B test, the Fusion arm should call `fusion_ask` or a specialized Fusion tool for
each substantive reasoning checkpoint:

- initial plan;
- architecture/design choice;
- debugging hypothesis;
- pre-edit implementation strategy;
- after a test failure;
- final self-review.

That is the closest practical MCP-based equivalent of “replace the LLM with Fusion panel”
while preserving normal Claude Code behavior.

## Prerequisites

Install and configure the plugin as described in the root README, then verify:

```bash
uv run fusion config validate
uv run fusion run-mock --task code_review --content "diff --git a/app.py b/app.py\n+def foo(): pass"
```

For live runs, set provider API keys in `.env`:

```bash
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=
```

Optional local-only/mock development:

```bash
export FUSION_DEFAULT_PROVIDER=mock
```

## Recommended run folder

For each benchmark case, create a folder like:

```text
evals/claude-code-runs/CASE_ID/
  task.md
  opus-output.md
  fusion-output.md
  verification.md
  metrics.json
  comparison.json
```

`task.md` should contain the exact user prompt and acceptance criteria. Use the same file for
both arms.

Example `metrics.json`:

```json
{
  "case_id": "retry-handling-001",
  "opus": {
    "label": "Claude Code + Opus",
    "model": "opus",
    "latency_ms": 0,
    "cost_usd": null,
    "notes": "Fill from Claude Code usage/cost UI or API billing export."
  },
  "fusion": {
    "label": "Claude Code + Fusion",
    "fusion_run_ids": [],
    "latency_ms": 0,
    "cost_usd": null,
    "notes": "Use Fusion MCP output usage/cost_comparison or fusion runs show RUN_ID."
  }
}
```

## Arm A: Claude Code + Opus/native model

Start a fresh Claude Code thread or reset context enough that the two arms are comparable.
Select Opus/native model in Claude Code.

Use this prompt template:

```text
You are running the OPUS baseline arm.

Do not call Fusion MCP tools in this arm.
Use normal Claude Code capabilities: inspect files, edit files, run tests, and iterate.

Task:
<paste the exact contents of task.md>

When done, provide:
1. Final answer / implementation summary.
2. Files changed.
3. Tests run and results.
4. Any remaining risks.
```

Record:

- final Claude Code answer in `opus-output.md`;
- changed files and test results in `verification.md`;
- wall-clock latency in `metrics.json`;
- Opus cost in `metrics.json` if available from Claude Code usage, billing export, or API logs.

If exact Opus cost is unavailable, leave `cost_usd: null`. The comparison tool will report the
cost winner as `unknown`.

## Arm B: Claude Code + Fusion MCP

Start from the same clean repository state and use the same `task.md`.

Use this prompt template:

```text
You are running the FUSION arm.

Keep all normal Claude Code capabilities. You may inspect files, edit files, run tests, and
iterate exactly as usual.

For substantive reasoning, use Fusion MCP as the model backend:
- call fusion_ask for general planning/implementation reasoning;
- call fusion_plan_feature for larger implementation plans;
- call fusion_debug_error after test failures or stack traces;
- call fusion_review_diff before finalizing a non-trivial diff.

After each Fusion call, use the returned answer like a model response and continue with normal
Claude Code editing/testing.

Task:
<paste the exact contents of task.md>

When done, provide:
1. Final answer / implementation summary.
2. Files changed.
3. Tests run and results.
4. Fusion run IDs used.
5. Fusion cost/usage summary from MCP outputs.
6. Any remaining risks.
```

Record:

- final Claude Code answer in `fusion-output.md`;
- Fusion `run_id` values from MCP outputs in `metrics.json`;
- test results in `verification.md`;
- wall-clock latency in `metrics.json`;
- Fusion cost from MCP outputs or from:

```bash
uv run fusion runs show RUN_ID
uv run fusion runs costs
```

For multiple Fusion calls, sum the Fusion run costs and use the whole Fusion-arm wall time.

## Compare the two arms inside Claude Code

After both arms are complete, call MCP tool `fusion_compare_claude_runs` with:

```json
{
  "task_prompt": "<contents of task.md>",
  "opus_output": "<contents of opus-output.md>",
  "fusion_output": "<contents of fusion-output.md>",
  "context": "<verification.md, relevant diffs, test results, and notes>",
  "opus_cost_usd": 0.42,
  "fusion_cost_usd": 0.07,
  "opus_latency_ms": 90000,
  "fusion_latency_ms": 45000,
  "opus_label": "Claude Code + Opus",
  "fusion_label": "Claude Code + Fusion",
  "include_raw_evals": true
}
```

The response includes:

- `display_markdown`: human-readable verdict;
- `result.better_arm`: quality winner from LLM-as-judge;
- `result.cheaper_arm`: cost winner if both costs are provided;
- `result.faster_arm`: latency winner if both latencies are provided;
- `result.opus` and `result.fusion`: per-arm scores, strengths, weaknesses, unsupported claims;
- eval run IDs for auditing.

## Compare the two arms from CLI

Use the CLI when outputs are stored in files:

```bash
uv run fusion compare-claude-runs \
  --task-file evals/claude-code-runs/CASE_ID/task.md \
  --opus-file evals/claude-code-runs/CASE_ID/opus-output.md \
  --fusion-file evals/claude-code-runs/CASE_ID/fusion-output.md \
  --context-file evals/claude-code-runs/CASE_ID/verification.md \
  --opus-cost 0.42 \
  --fusion-cost 0.07 \
  --opus-latency-ms 90000 \
  --fusion-latency-ms 45000 \
  > evals/claude-code-runs/CASE_ID/comparison.json
```

If a metric is unknown, omit it. The comparison will keep that dimension as `unknown`.

## How to interpret results

Use all dimensions together:

| Dimension | Meaning |
|-----------|---------|
| Better result | Fusion LLM-as-judge quality comparison on the same task/context |
| Cheaper | Requires measured cost for both arms |
| Faster | Requires measured wall-clock latency for both arms |
| Unsupported claims | Lower is better; check whether claims are grounded in repo evidence |
| Tests | Prefer the arm with passing, relevant verification |
| Iterations | Track how many Claude Code turns/tool loops were needed |

Fusion is successful when it preserves normal Claude Code capability and improves one or more
of these:

- lower cost;
- lower latency;
- better review/debug/planning quality;
- fewer missed risks;
- fewer unsupported claims;
- fewer correction turns.

## Common pitfalls

- Do not compare “Fusion answer only” against “Claude Code implemented result.” The Fusion arm
  should still let Claude Code edit and test normally.
- Do not count only Fusion MCP latency if Claude Code spent additional time applying edits.
  Use full wall-clock latency for each arm.
- Do not invent Opus cost. If unavailable, leave it unknown.
- Do not use different context between arms unless the benchmark explicitly measures that.
- Do not treat a cheaper result as better if tests fail or the answer is less grounded.

## Minimal manual checklist

```text
[ ] Same repo state for both arms
[ ] Same task prompt
[ ] Opus arm did not call Fusion
[ ] Fusion arm used Fusion for substantive reasoning
[ ] Claude Code could edit/run/test normally in both arms
[ ] Outputs captured
[ ] Costs captured or marked unknown
[ ] Latencies captured or marked unknown
[ ] Tests/verification captured
[ ] fusion_compare_claude_runs executed
```
