---
name: fusion-plan
description: Use for non-trivial feature implementation planning. Invokes fusion_plan_feature for phased plans with tests and risks.
---

# Fusion Implementation Planning

Use `fusion_plan_feature` when breaking down a feature that touches multiple modules.

## Input checklist

- `feature_description` — what to build (required)
- `constraints` — deadlines, compatibility, non-goals
- `context` — project/repo context
- `existing_patterns` — patterns to follow

## Output

Returns: `implementation_sequence`, `affected_modules`, `data_model_changes`, `api_changes`, `ui_changes`, `tests_to_add`, `risks`, `open_questions`, `confidence`, `evals`, `run_id`.

Fusion plans — Claude Code implements.
