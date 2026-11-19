---
description: Have Fusion's panel weigh architecture options and recommend one with tradeoffs and a migration plan
argument-hint: <decision, e.g. Redis or Postgres for the job queue>
allowed-tools: mcp__plugin_fusion_fusion__fusion_plan
---
Decide with Fusion: $ARGUMENTS

If no question was given, ask for the decision and the options.

Call the `fusion_plan` tool from the Fusion MCP server with `kind: "decision"`, the question in
`request`, the options in `options`, the constraints (scale, team, deadlines, existing stack) in
`constraints`, a short description of the system in `context`, and the files that show the current
design in `paths` (Fusion reads them itself).

Present the recommended option, its tradeoffs and risks, how reversible it is and the migration
steps. Say where you disagree with the panel and why. Report the cost line.
