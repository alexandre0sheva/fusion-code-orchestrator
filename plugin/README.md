# Fusion Code Orchestrator — Claude Code Plugin

Adds Fusion's multi-model panel to Claude Code through a local Python MCP server. Fusion is
side-effect free inside MCP: Claude Code applies edits and runs commands itself.

- **Setup and tool reference:** [docs/INTEGRATIONS.md](../docs/INTEGRATIONS.md)
- **Skills:** `fusion-orchestrator`, `fusion-plan`, `fusion-review`, `fusion-debug`, `fusion-decide`, `fusion-eval`
- **Commands:** `/fusion-plan`, `/fusion-review`, `/fusion-debug`, `/fusion-decide`, `/fusion-eval`, `/fusion-ab`
- **Manifest:** `plugin.json` (registers the MCP server `uv run fusion mcp`)
