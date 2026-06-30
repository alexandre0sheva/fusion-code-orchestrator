---
name: fusion-orchestrator
description: Multi-model orchestration advisor for complex coding workflows. Use for architecture decisions, code review, debugging, implementation planning, and answer evaluation. Do not use for trivial edits.
---

# Fusion Code Orchestrator

Fusion is an **advisory** multi-model orchestration engine. It analyzes context you provide and returns structured recommendations — it does not edit files or run shell commands.

## When to Use Fusion

Use Fusion MCP tools for:

- **Complex code review** — security-sensitive diffs, large changes, disagreement-prone areas
- **Debugging** — unclear root causes, production errors, multi-system failures
- **Architecture decisions** — trade-off analysis with multiple viable options
- **Implementation planning** — breaking down non-trivial features with test/risk notes
- **Answer evaluation** — scoring model-generated answers before acting on them

## When NOT to Use Fusion

Do **not** call Fusion for:

- Trivial one-line edits or formatting fixes
- Simple file reads or searches you can do directly
- Executing changes (Fusion is read-only/advisory)

## How to Call Fusion

1. Gather **focused context** — diffs, error messages, logs, relevant snippets
2. Call the appropriate MCP tool with concise inputs
3. Review structured output (findings, confidence, eval scores, disagreements)
4. Apply recommendations yourself in Claude Code

## MCP Tools

| Tool | Use when |
|------|----------|
| `fusion_review_diff` | Reviewing a git diff or patch |
| `fusion_debug_error` | Diagnosing errors with logs/stack traces |
| `fusion_decide_architecture` | Choosing between architecture options |
| `fusion_plan_feature` | Planning implementation of a feature |
| `fusion_eval_answer` | Evaluating quality of an LLM answer |

## Best Practices

- Include the **diff** when reviewing code changes
- Include **error message + stack trace + logs** when debugging
- Pass **changed file paths** when available (helps catch unsupported references)
- Treat Fusion output as **advisory** — verify before applying
- Check `confidence` and `evals.warnings` before high-risk actions
