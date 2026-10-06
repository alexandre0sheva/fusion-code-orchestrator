---
description: Run the same task with and without Fusion and compare the two results
argument-hint: <task>
allowed-tools: mcp__plugin_fusion_fusion__fusion_compare_claude_runs mcp__plugin_fusion_fusion__fusion_ask mcp__plugin_fusion_fusion__fusion_review_diff mcp__plugin_fusion_fusion__fusion_debug_error mcp__plugin_fusion_fusion__fusion_plan_feature
---
Run an A/B comparison on this task: $ARGUMENTS

If no task was given, ask for one. This spends more than a normal Fusion call: the comparison runs
two evaluation panels.

1. Arm A: do the task yourself without calling Fusion. Keep your answer, the test results, and the
   time it took.
2. Arm B: do the same task again from the same starting point, calling Fusion where it helps
   (`fusion_ask` for reasoning, or the review, debug and plan tools). Keep your answer, the test
   results and the time. Keep normal editing, shell and test tools enabled in both arms.
3. Call `fusion_compare_claude_runs` with the task, both outputs, the verification evidence and any
   measured cost and latency values.

Report the better result, the cheaper and faster arm, the quality scores, the weaknesses or
unsupported claims in each, and whether tests passed in each arm. One task proves little: say so.
