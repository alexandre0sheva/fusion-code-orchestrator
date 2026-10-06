---
description: Have Fusion's panel rank the likely root causes of an error and suggest how to verify each
argument-hint: [error message or leave empty for the latest error]
allowed-tools: mcp__plugin_fusion_fusion__fusion_debug_error
---
Debug with Fusion: $ARGUMENTS

If no error was given, use the most recent error in this session.

Call the `fusion_debug_error` tool from the Fusion MCP server with the error message in
`error_message`, the stack trace in `stack_trace`, relevant log lines in `logs`, the code around
the failure in `context`, and the environment (versions, OS) in `environment`.

Present the ranked causes and their verification steps. Run the cheapest verification for the most
likely cause first, and fix only after it confirms the cause. Report the cost line.
