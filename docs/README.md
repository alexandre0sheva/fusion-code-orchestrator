# Documentation

Operator and maintainer documentation for Fusion Code Orchestrator. Each document is the single
canonical home for its topic (see the docs contract in [../CONTRIBUTING.md](../CONTRIBUTING.md)).

| Document | Purpose |
|----------|---------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | MCP server, routing, fan-out, evals, telemetry and storage internals |
| [CONFIGURATION.md](CONFIGURATION.md) | Environment variables, YAML config, providers, budgets, fan-out, refinement |
| [COSTS.md](COSTS.md) | Pricing registry, baseline comparison, cost and latency limitations |
| [BENCHMARKING.md](BENCHMARKING.md) | How to measure Fusion against a single model |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Claude Code and Cursor setup, MCP tool reference |
| [CLAUDE_CODE_AB.md](CLAUDE_CODE_AB.md) | Manual Claude Code + Opus vs Claude Code + Fusion runbook |
| [superpowers/plans/2026-10-05-v0.2.0-roadmap.md](superpowers/plans/2026-10-05-v0.2.0-roadmap.md) | Task-by-task plan for 0.2.0 |

Core idea:

```text
Claude Code remains the executor.
Fusion MCP supplies cheaper multi-model reasoning, tracing, cost reporting, and evals.
```
