---
description: Have Fusion's panel draft an implementation plan for a feature
argument-hint: <feature description>
allowed-tools: mcp__plugin_fusion_fusion__fusion_plan
---
Plan this feature with Fusion: $ARGUMENTS

If no feature was given, ask for one.

Call the `fusion_plan` tool from the Fusion MCP server with `kind: "feature"` (the default), the
feature in `request`, constraints in `constraints`, the patterns the code already follows in
`existing_patterns`, what the project is in `context`, and the files the feature will touch in
`paths` (Fusion reads them itself). Look at the repository first so that those fields are
specific.

Turn the plan into concrete steps for this repository: the order of work, the files that change
and the tests to add. Flag anything in the plan that does not fit the code, and the open questions.
Report the cost line.
