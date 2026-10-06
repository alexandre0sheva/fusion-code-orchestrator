# Fusion Code Orchestrator — guide for agents and contributors

Python MCP server + CLI that gives Claude Code (and Cursor/Codex) a panel of cheap LLMs for
review, debugging, planning and answers, and measures whether that panel beats one big model on
cost, speed and quality. Claude Code stays the executor: Fusion tools return text, never edit
files or run commands. Current work: the v0.2.0 roadmap in
`docs/superpowers/plans/2026-10-05-v0.2.0-roadmap.md` (one task per session).

## Commands

```bash
uv sync --all-groups                      # install (Python 3.12+)
uv run pytest -q                          # offline tests, no API keys (mock provider)
uv run ruff check src tests evals         # lint
uv run mypy                               # strict typing
uv run pytest -m live                     # live API tests; cost money, run only when asked
```

Quality gate before saying a task is done: pytest, ruff and mypy all pass.

## Layout (`src/fusion/`)

`mcp_server/` tool handlers and schemas · `orchestration/` staged pipeline (`stages`, `ledger`,
`factory`), fanout, refine, synthesis, prompts · `providers/` direct API adapters + mock · `routing/` classifier, registry, policy, budget ·
`evals/` judge + deterministic checks · `benchmark/` shadow A/B and legacy compare · `bench/` studies (`fusion bench`) · `telemetry/`
cost and usage · `storage/` SQLite · `config/` YAML + env · `security/` redaction + prompt/output hardening · `cli/` Typer app.
Also: `plugin/` Claude Code plugin, `install/` client installers (`fusion install`), `evals/` datasets and runners, `tests/`.

## Conventions

- Python 3.12+, Pydantic v2 models, full type hints (mypy strict), async for all provider I/O.
- Never `print` to stdout in code reachable from `fusion mcp`: stdout carries JSON-RPC. Log to stderr.
- MCP orchestration tools stay side-effect free (no repo writes, no shell).
- Tests must run offline. Use `MockProvider` or `httpx.MockTransport`; mark real-API tests `@pytest.mark.live`.
- Never commit `.env`, API keys, `*.db`, or `bench-results/`. Never print keys.
- Ask before spending money on live APIs. The roadmap-wide live budget is $20 total.

## Docs contract

Each fact lives in one canonical file; update it and link to it, and delete stale text instead of
copying paragraphs. Read the docs-contract table in `CONTRIBUTING.md` before editing any docs.
`tests/test_docs_links.py` enforces links, anchors and the README size limit.

## Changelog rule

Everything in the roadmap ships as 0.2.0. Add one line per user-visible change under
`## [0.2.0] - Unreleased` in `CHANGELOG.md` (rules in `CONTRIBUTING.md`). Bump `pyproject.toml`
and `plugin/.claude-plugin/plugin.json` versions only in the release task.

## Git

Do not commit or push unless the owner asks. Finish a task by listing the changed files.
