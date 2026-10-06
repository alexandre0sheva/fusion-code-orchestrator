---
description: Have Fusion's panel score an answer for correctness, unsupported claims and missing points
argument-hint: [what to evaluate, or leave empty for your last answer]
allowed-tools: mcp__plugin_fusion_fusion__fusion_eval_answer
---
Evaluate with Fusion: $ARGUMENTS

If nothing was named, evaluate your previous answer.

Call the `fusion_eval_answer` tool from the Fusion MCP server with the original question in
`question`, the answer in `answer`, the context it was given in `context`, and the criteria it
must meet in `expected_criteria`. The tool judges the text only; it cannot run code.

Highlight unsupported claims and missing points. If the score is low, say what you would change.
