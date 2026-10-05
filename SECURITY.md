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

Known limitations:

- Redaction is pattern-based and can miss unusual secret formats.
- Cost comparison is only as accurate as provider token reporting and the pricing registry
  (see [docs/COSTS.md](docs/COSTS.md)).
- LLM-as-judge evaluations can fail or disagree; deterministic checks and warnings stay visible.
- Always verify Fusion recommendations against your codebase before applying them.

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
