---
description: Ask Fusion's model panel a self-contained coding question and get one answer with the points the models agree on
argument-hint: <question>
allowed-tools: mcp__plugin_fusion_fusion__fusion_ask
---
Ask the Fusion panel: $ARGUMENTS

If no question was given, ask the user what to ask before calling anything.

Call the `fusion_ask` tool from the Fusion MCP server with the question stated so that it stands
alone. Put what the panel cannot see in `context` (what the code does, the constraints) and short
excerpts, each prefixed with its path, in `file_snippets`. Do not paste whole files. Fusion reads
no files and runs nothing.

Then read the answer as a second opinion: keep the points the models agree on, check single-model
and disputed points against the code, and tell the user what you kept, what you rejected and the
cost line. Make any edits and run any tests yourself.
