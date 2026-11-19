---
description: Tell Fusion what became of its last answer (used, partly, wrong or ignored) so it can learn a per-project lesson
argument-hint: <used|partly|wrong|ignored> [why, in one line] [run id]
allowed-tools: mcp__plugin_fusion_fusion__fusion_feedback
---
Give Fusion feedback: $ARGUMENTS

Find the `run_id` of the Fusion answer this is about: the one named above, else the most recent
`run_id` in this conversation. If there is none, or the outcome is not clear, ask before calling
anything. Do not send feedback for an answer you have not acted on or checked.

Call the `fusion_feedback` tool from the Fusion MCP server with that `run_id`, the `outcome`
(`used`: you acted on the answer as given; `partly`: you used some of it; `wrong`: it was wrong or
misleading; `ignored`: you did not use it) and a `note` of one or two sentences naming the kind of
mistake or what made it useful. Leave out secrets and file contents: the note is redacted, but it
is stored.

Report what it says: whether a lesson was created, updated or dropped, and what it cost. With the
experience library off (`library.enabled`) it stores nothing and calls no model; say so.
