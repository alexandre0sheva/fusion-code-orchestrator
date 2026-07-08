---
name: fusion-orchestrator
description: Multi-model coding model for Claude Code. Use as a cheaper model-like reasoning panel for implementation guidance, architecture decisions, code review, debugging, planning, and answer evaluation.
---

# Fusion Code Orchestrator

Fusion is a **model-like multi-model orchestration engine**. Use it when Claude Code should get a cheaper, diverse model-panel answer before editing, testing, reviewing, or deciding. Fusion itself does not secretly edit files or run shell commands; Claude Code remains free to apply the answer and run normal tools.

## When to Use Fusion

Use Fusion MCP tools for:

- **General coding answers** — call `fusion_ask` when you want Fusion to act like a coding model
- **A/B evaluation** — call `fusion_compare_claude_runs` after Claude Code has produced both an Opus result and a Fusion-backed result
- **Complex code review** — security-sensitive diffs, large changes, disagreement-prone areas
- **Debugging** — unclear root causes, production errors, multi-system failures
- **Architecture decisions** — trade-off analysis with multiple viable options
- **Implementation planning** — breaking down non-trivial features with test/risk notes
- **Answer evaluation** — scoring model-generated answers before acting on them

## When NOT to Use Fusion

Do **not** call Fusion for:

- Trivial one-line edits or formatting fixes
- Simple file reads or searches you can do directly
- Direct filesystem or shell side effects inside the MCP server; Claude Code should execute those itself after using Fusion's answer

## How to Call Fusion

1. Gather **focused context** — diffs, error messages, logs, relevant snippets
2. Call the appropriate MCP tool with concise inputs
3. Review structured output (findings, confidence, eval scores, disagreements)
4. Apply the answer in Claude Code when it is useful

## MCP Tools

| Tool | Use when |
|------|----------|
| `fusion_ask` | Asking Fusion to answer a general coding task like a model |
| `fusion_review_diff` | Reviewing a git diff or patch |
| `fusion_debug_error` | Diagnosing errors with logs/stack traces |
| `fusion_decide_architecture` | Choosing between architecture options |
| `fusion_plan_feature` | Planning implementation of a feature |
| `fusion_eval_answer` | Evaluating quality of an LLM answer |
| `fusion_compare_claude_runs` | Comparing Claude Code + Opus vs Claude Code + Fusion outputs |
| `fusion_stats` | Showing cumulative savings and shadow A/B win-rate vs the frontier baseline |

## Budgets, Refinement, and Shadow A/B

- `budget: "high"` adds a mixture-of-agents refinement round (panel models revise
  after seeing anonymized peer answers) — use it for hard or high-stakes tasks.
- `shadow_baseline: true` on any orchestration tool also runs the real baseline model
  (Opus 4.8) on the same task and records a blind pairwise verdict. Use it when the
  user wants proof that Fusion matches big-model quality; it costs extra API money.
- When the user asks "how is Fusion doing" or wants savings/quality numbers, call
  `fusion_stats`.

## Best Practices

- Include the **diff** when reviewing code changes
- Include **error message + stack trace + logs** when debugging
- Pass **changed file paths** when available (helps catch unsupported references)
- Treat Fusion output like another model answer — useful, but verify before applying
- Check `confidence` and `evals.warnings` before high-risk actions
- For A/B tests, make Claude Code run both arms with the same prompt and context, then compare outputs with `fusion_compare_claude_runs`
