Run a Claude Code A/B comparison workflow.

Use this process:

1. Run the task once with Claude Code + Opus/native model and do not call Fusion.
2. Run the same task again with Claude Code + Fusion MCP:
   - use `fusion_ask` for general reasoning;
   - use specialized Fusion tools for plan/debug/review when relevant;
   - keep normal Claude Code file edits, shell commands, tests, and tools enabled.
3. Capture both final outputs, test results, costs if available, and wall-clock latencies.
4. Call `fusion_compare_claude_runs` with the original prompt, both outputs, verification
   evidence, and measured cost/latency values.

Report:
- better result;
- cheaper arm;
- faster arm;
- quality scores;
- important weaknesses or unsupported claims;
- whether tests passed in each arm.
