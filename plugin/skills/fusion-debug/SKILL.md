---
name: fusion-debug
description: Use when an error's cause is not obvious from its message after a first look, or a fix attempt failed. Do not use when the message already names the cause (a missing import, a typo, a wrong path).
user-invocable: false
---

# Fusion debug

Call `fusion_debug_error` with:

- `error_message` and `stack_trace`
- `logs`: only the relevant lines
- `context`: the code around the failure, and `recent_changes` if it started after an edit
- `environment`: language and library versions, OS

You get ranked hypotheses with a way to verify each. Run the cheapest check for the likeliest cause
first, and change code only after a check confirms the cause. Do not apply a fix just because the
panel agreed on it.
