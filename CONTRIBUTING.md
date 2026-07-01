# Contributing

Thanks for helping improve Fusion Code Orchestrator.

## Development setup

```bash
git clone https://github.com/alexandre0sheva/fusion-code-orchestrator.git
cd fusion-code-orchestrator
uv sync --all-groups
cp .env.example .env
```

Tests do not require real provider keys.

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy
```

## Local mock workflow

```bash
export FUSION_DEFAULT_PROVIDER=mock
uv run fusion config validate
uv run fusion run-mock --task code_review --content "diff --git a/app.py b/app.py\n+def foo(): pass"
```

## Pull request expectations

- Keep MCP orchestration tools side-effect free inside MCP.
- Claude Code should remain the executor for file edits, shell commands, and tests.
- Add or update tests for routing, fanout, costs, evals, MCP output, or storage changes.
- Keep docs aligned with user-facing behavior.
- Do not commit `.env`, API keys, local databases, or machine-specific paths.

## Security-sensitive changes

Changes touching redaction, provider payloads, storage, or command execution should include
tests and a short explanation of the risk model in the PR description.
