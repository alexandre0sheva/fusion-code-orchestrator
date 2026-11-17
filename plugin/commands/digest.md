---
description: Have Fusion's cheap models read large files or a diff and answer one question in a few hundred tokens
argument-hint: <question> [files or globs]
allowed-tools: mcp__plugin_fusion_fusion__fusion_digest
---
Digest with Fusion: $ARGUMENTS

If no question was given, ask what to find out before calling anything.

Call the `fusion_digest` tool from the Fusion MCP server with the question in `question`, the
files in `paths` or patterns in `globs` (such as `src/**/*.py`), or a git range in `diff_ref`.
Fusion reads them itself, read-only, so do not read or paste them first. Raise
`max_answer_tokens` (default 600) only if the question needs a longer answer.

Report the answer and the cost line, and the paths Fusion said it could not read. Treat the answer
as a summary to check: open the files it names before editing them.
