---
description: Have Fusion's panel write candidate patches and the checks that judge them, run them in a sandbox copy, and return the best patch as a diff for you to apply
argument-hint: <the change> [files] [test command]
allowed-tools: mcp__plugin_fusion_fusion__fusion_propose_patch mcp__plugin_fusion_fusion__fusion_select_patch
---
Propose a patch with Fusion: $ARGUMENTS

If no change was described, ask what to change before calling anything.

Call the `fusion_propose_patch` tool from the Fusion MCP server with the change in `task`, the
files it touches in `paths` (Fusion reads them itself, so do not paste them) and, when the project
has tests for this code, a command that runs them from the project root in `tests_cmd` (one that
needs no network, such as `python -m pytest -q tests/test_x.py`). Keep the default
`verify: "sandbox"`: Fusion runs the panel's checks and the tests in a temporary copy of the
project and only reads the project itself.

Fusion never applies the patch. Show the diff and the verification lines, then apply it yourself
(`git apply --check`, then `git apply`, or the same edits with your edit tool) and run the tests.
If the response says it fell back to host mode, it lists two candidates and their checks: run the
checks in a scratch worktree (never the working tree) on the code as it is and with each
candidate applied, then call `fusion_select_patch` with the `run_id`, `results` and `baseline`.
Report the cost line and any warnings.
