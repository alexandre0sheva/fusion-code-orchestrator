Use the Fusion Code Orchestrator MCP tool `fusion_review_diff` to perform multi-model code review on the current diff.

Pass:
- The diff content
- Changed file paths
- Review goals (security, tests, etc.)

Review the structured output and summarize critical findings for the user. If the user wants
the fixes applied, Claude Code should edit and test normally using the Fusion findings.
