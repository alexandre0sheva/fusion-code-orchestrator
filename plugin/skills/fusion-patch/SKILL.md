---
name: fusion-patch
description: Use when a bug fix or small, well-scoped change is costly to get wrong and its correctness can be checked by running code (a failing case, the project's tests), and a second set of eyes would help. Do not use for large refactors, multi-step features, edits you can make and verify directly, or code with no way to run it.
user-invocable: false
---

# Fusion patch

Call `fusion_propose_patch` with:

- `task`: the change, stated so it stands alone (the bug, the expected behaviour)
- `paths`: the files to change and the ones they depend on; Fusion reads them itself
- `tests_cmd`: a command that runs the relevant tests from the project root without network,
  when there are any
- leave `verify` at `"sandbox"`

Each panel member writes a whole patch and checks that would fail before a correct fix and pass
after; every patch runs against everyone's checks in a sandbox copy of the project, and the patch
that passes the most checks written by the others wins. Fusion only reads the project and never
applies the patch: you do, after reading the diff and the verification lines. Then run the tests
yourself.

In host mode (the sandbox is off or unavailable) you get two candidates and their checks. Run them
in a scratch worktree, never your working tree, and pass the results to `fusion_select_patch`.
A patch that passes the panel's checks can still be wrong where the checks were weak: review it
like any other patch.
