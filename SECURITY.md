# Security Policy

## Reporting a vulnerability

Please open a GitHub security advisory or private issue with enough detail to reproduce the
problem. Do not post live API keys, provider credentials, customer data, or private repository
content in a public issue.

Repository:

https://github.com/alexandre0sheva/fusion-code-orchestrator

## Security model

Fusion analyzes text you pass in and returns model-like answers. Claude Code remains the component
that edits files and executes shell commands.

| Control | Behavior |
|---------|----------|
| Direct providers | Fusion calls model providers through its own adapters; no aggregator sees prompts |
| Secret redaction | Input is scanned and common secrets are redacted before any external provider call |
| Sanitized logging | Sanitized input is stored separately from the original; raw prompt logging is off by default (`FUSION_LOG_RAW_PROMPTS=false`) |
| Deterministic safety checks | Flag secret leakage, dangerous shell commands and unsupported file references in answers |
| Judge skepticism | LLM-judge output is self-evaluated; deterministic checks run even if the judge fails |
| MCP boundary | MCP orchestration tools call pipelines only: no repo writes, no shell execution |
| Benchmark sandbox | `fusion bench` runs patches and hidden tests from a dataset you chose in a limited, scrubbed sandbox; never reachable through MCP (see below) |

Known limitations:

- Redaction is pattern-based and can miss unusual secret formats.
- Cost comparison is only as accurate as provider token reporting and the pricing registry
  (see [docs/COSTS.md](docs/COSTS.md)).
- LLM-as-judge evaluations can fail or disagree; deterministic checks and warnings stay visible.
- Always verify Fusion recommendations against your codebase before applying them.

## Benchmark sandbox

Benchmark mode can run code: the hidden tests of the `coding`, `frontend` and `performance`
datasets applied to a model's patch (`src/fusion/bench/sandbox.py`, scored by
`bench/scoring/coding.py` and `bench/scoring/artifact.py`), the evaluators that lint, compile and time
that code and read the page it builds (`bench/evaluators/`), and the `best-of-n-verified` strategy's
visible-test check. That code is written by models and was never reviewed by anyone.

| Control | Behavior |
|---------|----------|
| Where it runs | Only inside `fusion bench`, on a dataset you named. The MCP server and every real-mode path never call it, and the pipeline refuses the `verified` aggregator outside benchmark mode |
| Working directory | A fresh temporary directory per run, deleted afterwards. Absolute paths, `..`, `.git` and symlinks that leave it are rejected for patch files and when collecting results |
| Environment | Allow-list only: `PATH` and locale pass; `HOME` and `TMPDIR` point inside the sandbox. API keys, tokens and every other variable are not passed |
| Limits | A wall-clock timeout that kills the whole process group, CPU time, file size, no core dumps, memory on Linux, and capped output |
| Network and writes | Blocked where available: `sandbox-exec` on macOS (no network, no writes outside the sandbox), `unshare --net` on Linux (no network). `score.details["isolation"]` records what applied |
| Policy | `FUSION_SANDBOX_ISOLATION=auto` (default) runs with the limits alone and says so when isolation is missing; `require` refuses to run without it; `off` disables it |

**Evaluators and the agentic judge** run only in benchmark mode, on tasks from a dataset you named,
and are never reachable through MCP or `fusion ask`. Every evaluator that executes anything
(tests, build, lint, the performance benchmark) runs in the sandbox above, on a copy of the answer's
files; the directory it is given is only read. The agentic judge has read-only tools (no write,
no command, no network), its paths cannot leave the output it is looking at (`..`, absolute paths
and symlinks are refused), `run_evaluator` accepts only the task's own evaluators, and everything a
model wrote reaches it inside `<untrusted>` delimiters with a closing marker inside the text
defused, so an answer that says "give me full marks" is data, not an instruction. Its steps and
money are capped (12 steps, $0.50 per run) and every tool call is stored with the item.

**The browser is the exception to the sandbox.** Chromium has a sandbox of its own that cannot be
nested inside `sandbox-exec`, so the optional `[bench-visual]` evaluators contain the page
differently: it is served by a throwaway `http.server` bound to 127.0.0.1 over a *copy* of the
files; Chromium starts with every host but 127.0.0.1 unresolvable, and each request that is not for
that server (or a `data:` or `blob:` URL) is aborted and recorded as a failed request; the profile
is a temporary directory, service workers and downloads are off, and the capture has a time limit.
The page's JavaScript runs in Chromium under Chromium's own protections, with your user's
privileges. Without the extra nothing is opened.

Known limitations: this is a best-effort guard, not a security boundary. Without isolation (any
other OS, or `off`) a hostile patch can read files your account can read and use the network.
Memory limits are not enforced on macOS. Run studies on a machine or container where that is
acceptable, and prefer `require` in shared environments. A page that exploits a Chromium
vulnerability would not be stopped by anything here; keep the browser updated, and do not run
datasets you did not write.

## What not to commit

- `.env` files.
- API keys or bearer tokens.
- SQLite run databases.
- Local absolute paths.
- Private repository content or customer code.

## Validation before release

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy
uv run fusion config validate
```
