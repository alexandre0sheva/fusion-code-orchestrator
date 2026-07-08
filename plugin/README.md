# Fusion Code Orchestrator — Claude Code Plugin

This plugin adds multi-model orchestration tools to Claude Code via a local Python MCP server.

## Tools

| Tool | Purpose |
|------|---------|
| `fusion_ask` | General model-like coding answer from the Fusion panel |
| `fusion_review_diff` | Multi-model code review with synthesis |
| `fusion_debug_error` | Debug analysis with fix recommendations |
| `fusion_decide_architecture` | Architecture decision support |
| `fusion_plan_feature` | Implementation planning |
| `fusion_eval_answer` | Answer quality evaluation |
| `fusion_compare_claude_runs` | Compare Claude Code + Opus output against Claude Code + Fusion output |
| `fusion_stats` | Cumulative spend vs baseline, savings, and shadow A/B win-rate |

## Installation

1. Install the Python package (see root README).
2. Add this plugin directory to Claude Code plugins.
3. Configure provider API keys in `.env` for live models (optional; mock works offline).

## Skills & Commands

- **Skills**: `fusion-plan`, `fusion-review`, `fusion-debug`, `fusion-decide`, `fusion-eval`
- **Commands**: `/fusion-plan`, `/fusion-review`, `/fusion-debug`, `/fusion-decide`, `/fusion-eval`, `/fusion-ab`

## Security

Fusion is side-effect free inside MCP: it does not secretly edit files or run shell commands itself. Claude Code should use Fusion like a cheaper model response, then apply edits and run commands normally. All content is redacted before sending to external providers.
