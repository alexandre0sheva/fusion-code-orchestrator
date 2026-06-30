---
name: fusion-decide
description: Use for architecture trade-off decisions. Invokes fusion_decide_architecture for structured option analysis.
---

# Fusion Architecture Decisions

Use `fusion_decide_architecture` when choosing between approaches with real trade-offs.

## Input checklist

- `question` — the decision to make (required)
- `options` — list of options under consideration
- `constraints` — requirements, SLAs, team constraints
- `context` — current system context

## Output

Returns: `recommended_option`, `tradeoffs`, `rejected_options`, `risks`, `reversibility`, `migration_plan`, `test_strategy`, `confidence`, `evals`, `run_id`.

Not for trivial choices — use your judgment for obvious decisions.
