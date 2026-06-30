---
name: fusion-debug
description: Use when debugging unclear errors. Invokes fusion_debug_error for ranked hypotheses and fix strategies.
---

# Fusion Debug

Use `fusion_debug_error` when root cause is unclear or multiple systems are involved.

## Input checklist

- `error_message` — the exception or error text (required)
- `logs` — relevant log output
- `code_context` — snippets around the failure
- `recent_changes` — what changed recently
- `environment` — prod/staging, OS, versions

## Output

Returns: `most_likely_causes`, `ranked_hypotheses`, `verification_steps`, `minimal_fix_strategy`, `what_not_to_do`, `confidence`, `evals`, `run_id`.

Fusion advises — you execute fixes and verify in the repo.
