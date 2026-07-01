# Documentation

This directory contains operator and maintainer documentation for Fusion Code Orchestrator.

| Document | Purpose |
|----------|---------|
| [CLAUDE_CODE_AB.md](CLAUDE_CODE_AB.md) | Exact workflow for comparing Claude Code + Opus vs Claude Code + Fusion |
| [ARCHITECTURE.md](ARCHITECTURE.md) | MCP server, routing, fanout, evals, telemetry, and storage architecture |
| [COSTS.md](COSTS.md) | Pricing registry, baseline comparison, and cost/latency limitations |
| [PUBLICATION_CHECKLIST.md](PUBLICATION_CHECKLIST.md) | Public GitHub release checklist and private-info scan commands |

Core idea:

```text
Claude Code remains the executor.
Fusion MCP supplies cheaper multi-model reasoning, tracing, cost reporting, and evals.
```
