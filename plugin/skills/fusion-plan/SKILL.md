---
name: fusion-plan
description: Use at the start of a multi-file feature when the approach is not obvious. Do not use for small changes you can already sketch, or when a plan already exists.
user-invocable: false
---

# Fusion plan

Look at the repository first, then call `fusion_plan` (`kind: "feature"`) with the feature in
`request`, `constraints`, the conventions the code already follows in
`existing_patterns`, what the project is in `context`, and the files the feature touches in
`paths` (Fusion reads them itself; do not paste them).

Turn the result into concrete steps for this repository: the order of work, the files that change,
the tests to add. Flag anything that does not fit the code, and list the open questions for the
user before starting.
