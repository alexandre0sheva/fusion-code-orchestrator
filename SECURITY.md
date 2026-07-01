# Security Policy

## Reporting a vulnerability

Please open a GitHub security advisory or private issue with enough detail to reproduce the
problem. Do not post live API keys, provider credentials, customer data, or private repository
content in a public issue.

Repository:

https://github.com/alexandre0sheva/fusion-code-orchestrator

## Security model

- Fusion calls model providers directly through provider adapters.
- Common secrets are redacted before provider calls.
- Raw prompt logging is disabled by default.
- MCP orchestration tools do not perform hidden repository edits or shell commands.
- Claude Code remains the executor for normal file editing, shell commands, tests, and tool use.
- Optional agent benchmark mode is explicitly gated by `FUSION_AGENT_MODE=true`.

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
