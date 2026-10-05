# Fusion Code Orchestrator

A panel of small, cheap LLMs that answers coding questions for Claude Code (and Cursor) over MCP,
plus the instrumentation to find out whether that panel can match a single frontier model on cost,
speed and quality.

Claude Code stays the coding agent. Fusion is an advisor: it takes a diff, an error, a design
question or a feature request, asks a panel of cheap models in parallel, merges their answers, and
returns the result with token, cost and latency telemetry. It never edits files or runs commands.

> **Status: v0.1.0, alpha.** Fusion measures its own cost against a baseline model, but the claim
> "a cheap panel is as good as a frontier model" is not yet proven against ground truth. A
> benchmark mode that tests exactly that is the focus of 0.2.0; see the
> [roadmap](docs/superpowers/plans/2026-10-05-v0.2.0-roadmap.md).

## What Fusion is and is not

- It is a companion model panel behind MCP tools: review, debug, architecture, planning, general
  answers, answer evaluation, and cost/quality comparison against a baseline.
- It is not a hidden side-effect runner and it does not change Claude Code's model selector. Claude
  Code applies edits and runs tests itself, using Fusion's answer like another model response.
- It calls Anthropic, OpenAI and Google (plus optional Ollama / LM Studio) directly through provider
  adapters. There is no model aggregator in between, so routing, pricing, redaction and failure
  behaviour are visible in the code.

## Quickstart

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), and at least one of
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`.

```bash
git clone https://github.com/alexandre0sheva/fusion-code-orchestrator.git
cd fusion-code-orchestrator
uv sync --all-groups
cp .env.example .env          # then add your API keys
```

Verify with a live smoke test, or offline without keys:

```bash
uv run python evals/runners/compare_pipelines.py          # live, costs a few cents
uv run python evals/runners/compare_pipelines.py --mock   # offline
uv run fusion review-diff --file path/to/diff.patch       # review a diff
```

Connect it to Claude Code or Cursor: see [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md). In short,
register an MCP server that runs `uv run fusion mcp` from your clone, then restart the client.

## How it works

```text
Claude Code -> MCP tool -> redact -> route (strategy) -> cheap panel (parallel) -> [refine]
                                                   -> synthesize -> answer + cost/latency + run trace
```

Panel calls run concurrently and tolerate partial failure. Which models run is a *strategy*
(`fusion strategies list`); `panel-refine`, selected by the `high` budget, also has the panel models
revise their answers after seeing anonymized peer answers (mixture-of-agents). Every run is stored in
SQLite and compared against a baseline model; an opt-in shadow mode also calls the real baseline and
records a blind pairwise verdict. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## MCP tools

| Tool | Description |
|------|-------------|
| `fusion_ask` | General model-like coding answer |
| `fusion_review_diff` | Multi-model code review |
| `fusion_debug_error` | Debug analysis with fix recommendations |
| `fusion_decide_architecture` | Architecture decision support |
| `fusion_plan_feature` | Implementation planning |
| `fusion_eval_answer` | Answer quality evaluation |
| `fusion_compare_claude_runs` | Compare Claude Code + Opus vs Claude Code + Fusion |
| `fusion_stats` | Cumulative spend vs baseline, savings, shadow A/B win-rate |

Inputs, response envelope and budgets: [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md#mcp-tool-reference).

## Common commands

```bash
uv run fusion review-diff --file diff.patch      # review a diff
uv run fusion debug --error "TimeoutError: ..."  # debug an error
uv run fusion decide --question "Redis or memcache?"
uv run fusion plan --feature-file feature.md
uv run fusion eval-answer --question-file q.md --answer-file a.md
uv run fusion stats                              # cumulative spend, savings, shadow win-rate
uv run fusion runs list                          # run history (also: runs show RUN_ID)
uv run fusion config validate                    # check YAML config without calling providers
uv run fusion --help                             # everything else
```

The review, debug, decide, plan and eval commands accept `--mock` to run offline with the
deterministic mock provider.

## Documentation

| Topic | Document |
|-------|----------|
| Internals and data flow | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Env vars, YAML config, strategies, budgets | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Cost and pricing methodology | [docs/COSTS.md](docs/COSTS.md) |
| Measuring Fusion vs a single model | [docs/BENCHMARKING.md](docs/BENCHMARKING.md) |
| Claude Code / Cursor setup, tool reference | [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) |
| Manual Claude Code A/B runbook | [docs/CLAUDE_CODE_AB.md](docs/CLAUDE_CODE_AB.md) |
| Security model | [SECURITY.md](SECURITY.md) |
| Contributing and docs rules | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Release history | [CHANGELOG.md](CHANGELOG.md) |

## Project structure

```text
src/fusion/
  mcp_server/     MCP tool handlers and schemas
  orchestration/  pipelines, fan-out, refinement, synthesis, prompts
  providers/      direct API adapters (anthropic, openai, google, ollama, lmstudio, mock)
  routing/        classifier, model registry, budget, policy
  config/         YAML registries and env loading
  evals/          LLM judge and deterministic checks
  benchmark/      shadow A/B baseline comparison
  bench/          `fusion bench`: ground-truth studies, planner, simulated models
  telemetry/      usage, cost, baseline comparison
  security/       secret redaction
  storage/        SQLite run store
  cli/            Typer CLI
plugin/           Claude Code plugin (skills, commands, MCP config)
evals/            datasets and runners
tests/            offline pytest suite (mock providers)
```

## Development

```bash
uv sync --all-groups
uv run pytest -q && uv run ruff check src tests evals && uv run mypy
```

Tests run offline; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, see [LICENSE](LICENSE).
