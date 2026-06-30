---
name: fusion-review
description: Use when reviewing non-trivial code diffs. Invokes fusion_review_diff for multi-model code review with structured findings.
---

# Fusion Review

Use `fusion_review_diff` for multi-model code review when the change is non-trivial or security-sensitive.

## Input checklist

- `diff` — the git diff or patch (required)
- `changed_files` — list of changed paths when known
- `repo_context` — brief project/stack context
- `goals` — what to focus on (security, performance, tests)

## Output

Returns structured JSON: `summary`, `critical_findings`, `recommended_changes`, `test_plan`, `consensus`, `disagreements`, `confidence`, `evals`, `routing`, `run_id`.

Do not use for trivial formatting or single-line fixes — review those directly.
