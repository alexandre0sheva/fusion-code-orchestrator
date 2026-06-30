---
name: fusion-eval
description: Use to evaluate LLM answer quality before acting on it. Invokes fusion_eval_answer with rubric-based scoring.
---

# Fusion Answer Evaluation

Use `fusion_eval_answer` to score an answer before relying on it for high-impact work.

## Input checklist

- `answer` — the answer to evaluate (required)
- `question` — original question or task
- `context` — context the answer was based on
- `rubric` or `expected_criteria` — what good looks like

## Output

Returns: `score`, `strengths`, `weaknesses`, `unsupported_claims`, `missing_points`, `safer_answer`, `confidence`, `evals`, `run_id`.

Check `unsupported_claims` carefully — Fusion flags claims not grounded in provided context.
