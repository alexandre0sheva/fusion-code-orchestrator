# Fusion: Claude Code plugin

Adds Fusion's panel of cheap models to Claude Code: slash commands, skills that say when to call
it, a `fusion-advisor` subagent, and the MCP server (started with `uvx`, no clone needed). Fusion
returns text only; Claude Code applies edits and runs commands itself.

```text
plugin/
  .claude-plugin/plugin.json   manifest (name `fusion`, so commands are /fusion:*)
  .mcp.json                    the server: uvx --python ">=3.11" --managed-python --from git+<repo> fusion mcp
  commands/                    /fusion:ask review debug plan decide eval stats bench ab
  skills/                      fusion-orchestrator, fusion-review, -debug, -decide, -plan, -eval
  agents/fusion-advisor.md     returns a short verdict; can read files and call Fusion only
```

The marketplace entry that lets `/plugin marketplace add alexandre0sheva/fusion-code-orchestrator`
find it is `../.claude-plugin/marketplace.json`.

- **Install, keys, options, smoke test, troubleshooting:** [docs/INTEGRATIONS.md](../docs/INTEGRATIONS.md#claude-code)
- **Tool reference:** [docs/INTEGRATIONS.md](../docs/INTEGRATIONS.md#mcp-tool-reference)
- **Try it without installing:** `claude --plugin-dir ./plugin`
- **Check the manifests:** `claude plugin validate ./plugin` and `claude plugin validate .`
