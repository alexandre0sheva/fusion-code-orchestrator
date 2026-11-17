---
description: Ask Fusion's model panel a self-contained coding question and get one answer with the points the models agree on
argument-hint: <question>
allowed-tools: mcp__plugin_fusion_fusion__fusion_ask
---
Ask the Fusion panel: $ARGUMENTS

If no question was given, ask the user what to ask before calling anything.

Call the `fusion_ask` tool from the Fusion MCP server with the question stated so that it stands
alone. Name the relevant files in `paths` (or `globs`): Fusion reads them itself, read-only, so
you do not paste them. Put what the panel cannot see in the code in `context` (the goal, the
constraints). Fusion runs nothing.

Then read the answer as a second opinion: keep the points the models agree on, check single-model
and disputed points against the code, and tell the user what you kept, what you rejected and the
cost line. Make any edits and run any tests yourself.
