---
name: fusion-decide
description: Use before committing to a hard-to-reverse design choice with several viable options (storage, queue, framework, service boundaries). Do not use for choices that are easy to reverse or that project conventions already settle.
user-invocable: false
---

# Fusion decide

Call `fusion_decide_architecture` with the decision in `question`, the candidates in `options`,
`constraints` (scale, team, deadline, existing stack) and a short `context` description of the
system.

Present the recommended option with its tradeoffs, risks, reversibility and migration steps, and
say where you disagree with the panel. A recommendation from cheap models is an input to the
decision, not the decision.
