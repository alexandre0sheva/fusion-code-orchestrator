---
description: Have Fusion's panel review the current diff and report findings grouped by how many models agree
argument-hint: [focus, e.g. security]
allowed-tools: mcp__plugin_fusion_fusion__fusion_review_diff Bash(git diff *) Bash(git status *)
---
Review the current change with Fusion. Focus: $ARGUMENTS

1. Check there is a change: `git status --short`. If nothing changed, say so and stop. The range
   is `HEAD` (staged and unstaged work) unless the user named a branch or commit range.
2. Call the `fusion_review_diff` tool from the Fusion MCP server with that range in `diff_ref`
   (Fusion reads the `git diff` itself; do not paste it), new untracked files in `paths`, the focus
   (if any) in `goals`, and a few lines on what the project is and what the change is for in
   `context`.
3. Check each finding against the code before reporting it. Report the critical ones first, say
   which you confirmed and which you rejected and why, and give the cost line.

Do not fix anything unless the user asks; if they do, edit and test as usual.
