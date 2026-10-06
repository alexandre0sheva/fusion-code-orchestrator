# Fusion Code Orchestrator

[![CI](https://github.com/alexandre0sheva/fusion-code-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/alexandre0sheva/fusion-code-orchestrator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11 | 3.12 | 3.13 | 3.14](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)](pyproject.toml)

A panel of small, cheap LLMs that answers coding questions for Claude Code (and Cursor) over MCP,
plus the instrumentation to find out whether that panel can match a single frontier model on cost,
speed and quality.

Claude Code stays the coding agent. Fusion is an advisor: it takes a diff, an error, a design
question or a feature request, asks a panel of cheap models in parallel, merges their answers, and
returns the result with token, cost and latency telemetry. It never edits files or runs commands.

> **Thesis, tested in v0.2.0 (alpha):** a panel of cheap models can give advice close to a frontier
> model's for a fraction of the cost. The measured answer, below, is more modest than the pitch:
> **cheaper, yes; faster, no; better, not shown.** See the [changelog](CHANGELOG.md) for what is in
> the release.

## Does a cheap panel match a frontier model?

On 34 held-out tasks with ground truth (code review, debugging, architecture, planning, coding,
frontend, performance), 2 repeats each, $9.61 of live API spend:

| | Quality (95% CI) | Cost per solved task | Median seconds |
|---|---|---|---|
| Claude Opus 5.5 alone | 0.64 [0.53, 0.74] | $0.119 | 19.8 |
| GPT-6.1 Sol alone | 0.65 [0.54, 0.76] | $0.030 | 34.0 |
| Claude Haiku 4.5 alone | 0.58 [0.46, 0.70] | $0.029 | 15.5 |
| GPT-6 Luna alone | 0.64 [0.52, 0.75] | $0.001 | 12.3 |
| **Fusion `panel-duo` (default)** | 0.67 [0.53, 0.80] | $0.036 | 30.4 |
| Fusion `panel-cheap` (three models) | 0.66 [0.52, 0.79] | $0.049 | 32.2 |

![Quality against cost per task](docs/assets/benchmark-cost-quality.svg)

- **Cheaper: yes.** Fusion costs about 0.3x Opus per solved task (interval 0.21 to 0.41).
- **Faster: no.** It is about 1.5x slower, because it waits for several models and then merges.
- **Better or not worse than Opus: the study cannot tell.** The quality difference is +0.03 with
  an interval of about plus or minus 0.12, wider than the 0.03 margin. Nothing separates any of the
  arms on quality, and a single cheap model (Luna) scored as well as the panel at about 1/30 of its
  cost. So far the panel's case rests on cost against a frontier model, not on beating a cheap one.

Small, synthetic and LLM-judged in part: read the limitations before quoting a number.
Everything, with the method and the unedited verdicts, is in
[docs/BENCHMARK_RESULTS.md](docs/BENCHMARK_RESULTS.md) and the
[interactive report](docs/benchmark-report.html).

## Limitations

- The study is small (34 tasks, 2 repeats) and its intervals are wide, so "not worse than Opus" is
  *inconclusive*, not *equal*. The tasks are synthetic and not yet reviewed by a person.
- Fusion is slower than one model: it waits for several, then merges.
- Redaction is pattern-based and can miss a secret; answers are advice to check, not instructions.
- Not on PyPI: install from GitHub (`uvx --from git+https://github.com/alexandre0sheva/fusion-code-orchestrator`;
  append `@v0.2.0` to pin the release).

The full list is in the [changelog](CHANGELOG.md#known-limitations) and [SECURITY.md](SECURITY.md#known-limitations).

## What Fusion is and is not

- It is a companion model panel behind MCP tools: review, debug, architecture, planning, general
  answers, answer evaluation, and cost/quality comparison against a baseline.
- It is not a hidden side-effect runner and it does not change Claude Code's model selector. Claude
  Code applies edits and runs tests itself, using Fusion's answer like another model response.
- It calls Anthropic, OpenAI and Google (plus optional Ollama / LM Studio) directly through provider
  adapters. There is no model aggregator in between, so routing, pricing, redaction and failure
  behaviour are visible in the code.

## Quickstart

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), and at least one of
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

**Use it in Claude Code** (no clone needed; three commands):

```bash
export ANTHROPIC_API_KEY=...   # and/or OPENAI_API_KEY, GOOGLE_API_KEY
uvx --python '>=3.11' --from git+https://github.com/alexandre0sheva/fusion-code-orchestrator fusion install claude-code --plugin
claude                         # then try /fusion:review, /fusion:debug or /fusion:ask
```

Options, the server-only install, Cursor (`fusion install cursor`), Codex (`fusion install codex`) and troubleshooting: [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md).

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
uv run fusion doctor                              # is this machine ready? (keys, config, MCP server)
uv run fusion ask "Why does this retry loop never back off?" -f src/http.py
git diff main | uv run fusion review-diff -f -    # review a diff
uv run fusion debug --file trace.txt              # debug an error
uv run fusion stats                               # cumulative spend, savings, shadow win-rate
uv run fusion dashboard                           # local web view of spend, runs, benchmarks (read-only)
uv run fusion --help                              # everything else
```

![The dashboard's overview: spend against the baseline model](docs/assets/dashboard.png)

*`fusion dashboard` (synthetic demo data). Details: [docs/CONFIGURATION.md](docs/CONFIGURATION.md#dashboard).*

Every run command takes `--mock` (offline, no keys), `--json`, `--strategy` and `--max-cost`.
Flags, exit codes, the live view and shell completion: [docs/CONFIGURATION.md](docs/CONFIGURATION.md#command-line).

## Documentation

| Topic | Document |
|-------|----------|
| Internals and data flow | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Env vars, YAML config, strategies, budgets | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Cost and pricing methodology | [docs/COSTS.md](docs/COSTS.md) |
| Measuring Fusion vs a single model | [docs/BENCHMARKING.md](docs/BENCHMARKING.md) |
| Claude Code / Cursor / Codex setup, tool reference | [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) |
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
  security/       secret redaction, untrusted-text delimiters, output cleaning
  storage/        SQLite run store
  cli/            Typer CLI
  dashboard/      `fusion dashboard`: read-only local web UI
  install/        `fusion install`: one-command client setup
plugin/           Claude Code plugin (commands, skills, subagent, MCP config)
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
