---
description: Have Fusion's panel review the current diff and report findings grouped by how many models agree
argument-hint: [focus, e.g. security]
allowed-tools: mcp__plugin_fusion_fusion__fusion_review_diff Bash(git diff *) Bash(git status *)
---
Review the current change with Fusion. Focus: $ARGUMENTS

1. Get the diff: `git diff HEAD` (staged and unstaged). If it is empty, say so and stop. If the
   user named a branch or commit range, use that instead.
2. Call the `fusion_review_diff` tool from the Fusion MCP server with the diff in `diff`, the
   changed paths in `changed_files`, the focus (if any) in `goals`, and a few lines on what the
   project is and what the change is for in `context`.
3. Check each finding against the code before reporting it. Report the critical ones first, say
   which you confirmed and which you rejected and why, and give the cost line.

Do not fix anything unless the user asks; if they do, edit and test as usual.
