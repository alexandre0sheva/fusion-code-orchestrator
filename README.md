# Fusion Code Orchestrator

Multi-model orchestration and evaluation engine for coding workflows. Exposed as a **Python MCP server** for Claude Code and Cursor, with direct provider adapters (no OpenRouter).

Claude Code remains the main coding agent. Fusion gives it external tools for architecture decisions, code review, debugging hypotheses, implementation planning, and model answer evaluation — using **real multi-model panels** (Anthropic, OpenAI, Google) by default.

## Features

- **8 MCP tools**: general Fusion answer, code review, debug, architecture, planning, answer evaluation, Claude-run comparison, optional isolated implementation benchmark
- **Multi-model fan-out**: concurrent panel calls with disagreement analysis
- **Hybrid evaluation**: LLM-as-judge + deterministic safety/completeness checks
- **Direct provider adapters**: Anthropic, OpenAI, Google (Ollama/LM Studio optional)
- **YAML model registry** with cost/quality/latency tiers and routing policies
- **Budget-aware routing**: low / medium / high / local_only
- **Secret redaction**: API keys, tokens, passwords redacted before external calls
- **SQLite logging**: full run traces with costs and latency
- **Model-like MCP boundary**: Fusion answers like a cheaper model panel; Claude Code applies edits and runs commands

## What Fusion is and is not

Fusion is a Claude Code companion model, not a hidden side-effect runner. Claude Code calls
Fusion through MCP when a task benefits from a cheaper multi-model answer, external review,
architecture trade-offs, debugging hypotheses, implementation planning, or answer evaluation.
Fusion returns an answer, structured data, usage telemetry, and trace IDs; Claude Code can then
edit files and run commands normally.

Fusion does **not** use OpenRouter. Provider adapters call Anthropic, OpenAI, Google, Ollama,
or LM Studio directly. This keeps routing, pricing, redaction, and provider failure behavior
visible in the codebase and avoids sending prompts through a model aggregator.

## Documentation

- [Claude Code A/B runbook](docs/CLAUDE_CODE_AB.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Costs and baseline comparison](docs/COSTS.md)
- [Publication checklist](docs/PUBLICATION_CHECKLIST.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended)
- At least one cloud API key: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and/or `GOOGLE_API_KEY`

### Install

```bash
git clone https://github.com/alexandre0sheva/fusion-code-orchestrator.git
cd fusion-code-orchestrator
uv sync --all-groups
cp .env.example .env
# Edit .env and add your API keys
```

Fusion loads `.env` automatically when you run the CLI or MCP server.

### Verify setup (live providers)

```bash
# Quick smoke test across task types (uses real API calls)
uv run python evals/runners/compare_pipelines.py

# Code review from a diff file
uv run fusion review-diff --file path/to/diff.patch

# Inspect run history
uv run fusion runs list
uv run fusion runs show RUN_ID
uv run fusion runs compare-baseline RUN_ID
uv run fusion runs export --format jsonl
```

### Run tests (offline, no API keys)

```bash
uv run pytest
uv run ruff check src tests
uv run mypy
```

Tests automatically use `MockProvider` via `FUSION_DEFAULT_PROVIDER=mock` in `tests/conftest.py`. Production defaults never use mocks.

### Offline / mock mode (optional)

For development without API keys:

```bash
export FUSION_DEFAULT_PROVIDER=mock
uv run fusion review-diff --file my.patch --mock
uv run fusion run-mock --task code_review --content "diff: + def foo(): pass"
uv run python evals/runners/run_offline_eval.py --dataset code_review --mock
```

## Claude Code A/B comparisons

Primary workflow:

1. Run the same prompt in Claude Code with Opus/native model.
2. Run the same prompt in Claude Code while using `fusion_ask` or a specialized Fusion tool for reasoning.
3. Let Claude Code apply edits and run tests in both cases.
4. Call `fusion_compare_claude_runs` with the original prompt, both outputs, optional costs, optional latencies, and verification evidence.

This keeps all repository reads, edits, tool calls, and shell commands in regular Claude Code.
Fusion only replaces the expensive reasoning model with a cheaper panel and provides eval
instrumentation.

### Optional isolated agent harness

There is also an older lab harness that runs direct-provider agents in temporary workspace
copies. It is useful for API-level experiments, but it is **not** the main Claude Code
replacement workflow.

Enable it explicitly:

```bash
# .env
FUSION_AGENT_MODE=true
FUSION_WORKSPACE_ROOT=/absolute/path/to/your/project
```

Agent operations are restricted to `FUSION_WORKSPACE_ROOT`. Secrets in commands are blocked.

### One-shot isolated A/B: implement with Opus API agent vs Fusion API agent

From **Claude Code** (single MCP call returns everything, using the isolated harness):

```text
Call fusion_compare_implement with:
- task: "Add a FUSION_AGENT_MODE section to README"
- workspace_root: "/path/to/fusion-code-orchestrator"
- verify_command: "uv run pytest tests/test_agent.py -q"
```

From **CLI**:

```bash
uv run fusion compare-implement \
  --task "Add caching note to README" \
  --workspace /path/to/project \
  --verify-command "uv run pytest -q"
```

**Response includes:**

| Field | Meaning |
|-------|---------|
| `opus.usage.cost_usd` / `input_tokens` / `output_tokens` / `latency_ms` | Direct Opus API agent arm |
| `fusion.usage.*` | Fusion executor agent arm |
| `fusion.orchestration.*` | Multi-model plan step (panel + synthesis) |
| `cost_delta_usd` | Fusion total − Opus total (negative = Fusion cheaper) |
| `cheaper_arm` / `faster_arm` | Quick comparison |
| `opus.summary` / `fusion.summary` | What each arm implemented |
| `opus.files_changed` / `fusion.files_changed` | Files touched in isolated workspace copies |

Each arm runs in a **temporary copy** of your workspace — your repo is not modified.

**Note:** Opus arm uses Anthropic API (`claude-opus` by default), not Claude Code session billing. This gives a fair API-cost comparison with Fusion's direct provider calls.

## Connect to Claude Code

### 1. Install the Python package

Follow [Install](#install) above and confirm your `.env` has API keys.

### 2. Add the plugin

**Option A — Local plugin directory (recommended)**

In Claude Code settings, add a plugin source pointing to:

```text
/path/to/fusion-code-orchestrator/plugin
```

The plugin manifest (`plugin/plugin.json`) registers the MCP server:

```json
{
  "mcpServers": {
    "fusion": {
      "command": "uv",
      "args": ["run", "fusion", "mcp"],
      "cwd": "/absolute/path/to/fusion-code-orchestrator"
    }
  }
}
```

Set `cwd` to your clone path so `uv` finds the project and loads `.env`.

**Option B — Manual MCP config**

Add to your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "fusion": {
      "command": "uv",
      "args": ["run", "fusion", "mcp"],
      "cwd": "/absolute/path/to/fusion-code-orchestrator"
    }
  }
}
```

### 3. Restart Claude Code

Restart to load the MCP server and skills in `plugin/skills/`.

### 4. Use Fusion from Claude Code

Skills teach Claude when to call Fusion vs. handling trivial edits directly:

| Skill | When Claude should use it |
|-------|---------------------------|
| `fusion-review` | Complex or security-sensitive diffs |
| `fusion-debug` | Unclear root causes, production errors |
| `fusion-decide` | Architecture trade-offs |
| `fusion-plan` | Non-trivial feature implementation plans |
| `fusion-eval` | Scoring answers before acting on them |

Example prompts inside Claude Code:

- “Use Fusion to review this diff before I merge.”
- “Call `fusion_debug_error` with this stack trace and logs.”
- “Run `fusion_decide_architecture` for Redis vs Postgres caching.”

Fusion returns structured JSON with answers/findings, confidence, hybrid evals, routing metadata, and cost/latency. Claude Code should use that output like another model response and then apply changes or run tests itself.

## Cost and usage comparison

Every provider response is normalized into usage telemetry:

- provider name, model alias, and provider model ID;
- input, output, cached input, reasoning, and total token counts when available;
- actual provider cost when returned, otherwise configured estimated cost;
- latency, success/failure, and structured error details.

Pricing lives in `src/fusion/config/pricing.yaml`. Each entry is in USD per one million
tokens and has an `is_estimate` flag. If token usage or pricing is unavailable, Fusion marks
cost as unknown rather than inventing a precise value.

The default baseline lives in `src/fusion/config/baseline.yaml`:

```yaml
baseline:
  name: "Opus 4.8"
  provider: "anthropic"
  model_id: "claude-opus-4-8"
  pricing_alias: "anthropic.claude-opus-4-8"
  enabled: true
  estimate_strategy: "same_input_and_output_tokens"
```

The baseline estimate asks: “What would the same input/output token volume cost on this
single frontier model?” It does not claim the baseline would produce the same number of
tokens or latency in a live run. Baseline latency is reported as unknown unless the baseline
is actually called by an explicit benchmark.

Claude Code-facing output includes a compact section:

```text
Cost & usage:
- Fusion cost: $0.0180 estimated
- Opus 4.8 baseline estimate: $0.0710 estimated
- Estimated savings: $0.0530 / 74.6%
- Fusion wall time: 8.4s
- Panel: 4 models, 3 succeeded, 1 failed
```

The same response includes machine-readable `usage` and `cost_comparison` objects.

## Parallel panel fanout

Panel calls run concurrently inside a normal blocking MCP request. Claude Code receives one
response, but Fusion internally starts selected panel model calls together with async fanout.

Fanout config lives in `src/fusion/config/routing_policies.yaml`:

```yaml
fanout:
  max_concurrency: 6
  per_model_timeout_seconds: 45
  global_timeout_seconds: 60
  min_successful_responses: 2
  cancel_on_global_timeout: true
  allow_partial_results: true
```

Fusion preserves partial panel results. A failed or timed-out model produces warnings and
usage records, but the run continues when quorum is met. If quorum is not met, Fusion returns
a structured diagnostic instead of pretending synthesis succeeded.

## Configuration and validation

Core config files:

- `src/fusion/config/default_models.yaml` — model registry and provider aliases.
- `src/fusion/config/routing_policies.yaml` — task routing, budgets, and fanout.
- `src/fusion/config/pricing.yaml` — pricing registry with estimate flags.
- `src/fusion/config/baseline.yaml` — single frontier baseline comparison model.
- `.env` — provider keys and local runtime toggles.

Validate config without calling providers:

```bash
uv run fusion config validate
uv run fusion config validate --strict
```

Strict mode fails when enabled cloud providers are missing API-key environment variables.
Non-strict mode reports those as warnings so mock/local development remains easy.

### Connect to Cursor

Add to `.cursor/mcp.json` in this repo (or Cursor MCP settings):

```json
{
  "mcpServers": {
    "fusion": {
      "command": "uv",
      "args": ["run", "fusion", "mcp"],
      "cwd": "/absolute/path/to/fusion-code-orchestrator"
    }
  }
}
```

**Do not run `uv run fusion mcp` manually in a terminal** — it speaks JSON-RPC on stdin/stdout and is meant to be spawned by Cursor or Claude Code. Pressing Enter in that terminal sends invalid input and triggers `Invalid JSON: EOF` errors. To verify providers work, use:

```bash
uv run python evals/runners/compare_pipelines.py
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `fusion_ask` | General model-like coding answer using the Fusion panel |
| `fusion_review_diff` | Multi-model code review with synthesis |
| `fusion_debug_error` | Debug analysis with fix recommendations |
| `fusion_decide_architecture` | Architecture decision support |
| `fusion_plan_feature` | Implementation planning |
| `fusion_eval_answer` | Answer quality evaluation |
| `fusion_compare_claude_runs` | Compare Claude Code + Opus vs Claude Code + Fusion outputs |

Each tool accepts an optional `budget` field: `low`, `medium`, `high`, or `local_only`.

Each tool returns both top-level task-specific fields and a consistent MCP envelope:

| Field | Meaning |
|-------|---------|
| `display_markdown` | Compact Claude Code-facing summary with recommendation, confidence, cost, usage, and caveats |
| `result` | Structured task-specific result object |
| `evals` | Context, per-answer, disagreement, judge, and final eval data |
| `usage` | Per-model token, cost, latency, and failure telemetry |
| `cost_comparison` | Fusion vs configured baseline estimate |
| `routing` | Selected panel, judge, synthesizer, risk, complexity, and routing reasons |
| `warnings` | Timeouts, provider failures, config caveats, and budget warnings |
| `run_id` | SQLite trace ID for `fusion runs show RUN_ID` |

## How orchestration works

```
Claude Code → MCP Tools → Orchestration Pipeline
                              ├── Secret Redaction
                              ├── Task Classification & Routing
                              ├── Context Eval
                              ├── Panel Fan-out (concurrent, real models)
                              ├── Response Eval (LLM judge + deterministic)
                              ├── Disagreement Analysis
                              ├── Synthesis
                              ├── Final Eval
                              └── SQLite Logging
```

## Security model and limitations

Fusion redacts common secrets before provider calls and persists sanitized input separately
from original input. The MCP orchestration tools do not perform hidden repository mutations:
they do not run shell commands in user repositories or edit user files themselves. This does
not block Claude Code from doing normal coding-agent work after receiving Fusion's answer.
The separate benchmark/agent mode is gated by `FUSION_AGENT_MODE=true` and workspace restrictions.

Known limitations:

- Cost comparison is only as accurate as provider token reporting and `pricing.yaml`.
- Baseline cost is an estimate unless an explicit benchmark calls the baseline model.
- LLM-as-judge evals can fail or disagree; deterministic checks and warnings remain visible.
- Local model quality and latency depend on the user’s Ollama/LM Studio setup.

See also [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/COSTS.md](docs/COSTS.md).
For the exact Claude Code A/B workflow, see
[docs/CLAUDE_CODE_AB.md](docs/CLAUDE_CODE_AB.md).

### Default model panel (medium budget)

| Role | Models |
|------|--------|
| Code review panel | Claude Sonnet, GPT-5.4-mini (security role), Gemini Flash |
| Debug panel | Claude Sonnet, Gemini Flash |
| Judge | Gemini Flash (JSON scoring) |
| Synthesizer | Claude Sonnet |

Panel members receive **real role prompts** (coding reviewer, security reviewer, debugger, architect, etc.) defined in `src/fusion/orchestration/prompts.py` — not mock personalities.

### Routing policies

Edit `src/fusion/config/routing_policies.yaml` to change panel composition, judge, synthesizer, and per-budget overrides.

Edit `src/fusion/config/default_models.yaml` for model IDs, tiers, and enable/disable flags.

## Supported Providers

| Provider | Env vars | Default |
|----------|----------|---------|
| **Anthropic** | `ANTHROPIC_API_KEY` | Enabled when key is set |
| **OpenAI** | `OPENAI_API_KEY` | Enabled when key is set |
| **Google** | `GOOGLE_API_KEY` | Enabled when key is set |
| **Ollama** | `OLLAMA_ENABLED=true`, `OLLAMA_BASE_URL` | Optional, off by default |
| **LM Studio** | `LMSTUDIO_ENABLED=true`, `LMSTUDIO_BASE_URL` | Optional, off by default |
| **Mock** | `FUSION_DEFAULT_PROVIDER=mock` or `--mock` | Tests and offline dev only |

Ollama and LM Studio **never block** cloud usage. They are registered only when explicitly enabled. If `local_only` budget is requested but no local models are configured, Fusion falls back to cloud models with a warning.

To use local models:

```bash
export OLLAMA_ENABLED=true
ollama serve
ollama pull llama3.2
# Set ollama-llama enabled: true in default_models.yaml
```

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key | — |
| `OPENAI_API_KEY` | OpenAI API key | — |
| `GOOGLE_API_KEY` | Google API key | — |
| `OLLAMA_ENABLED` | Enable Ollama provider | `false` |
| `OLLAMA_BASE_URL` | Ollama endpoint | `http://localhost:11434` |
| `LMSTUDIO_ENABLED` | Enable LM Studio provider | `false` |
| `LMSTUDIO_BASE_URL` | LM Studio endpoint | `http://localhost:1234/v1` |
| `FUSION_DB_PATH` | SQLite database path | `./fusion_runs.db` |
| `FUSION_LOG_RAW_PROMPTS` | Log unsanitized prompts (dangerous) | `false` |
| `FUSION_DEFAULT_PROVIDER` | Set to `mock` for offline mode | unset (live) |

## CLI Commands

```bash
uv run fusion mcp                                    # Start MCP server
uv run fusion review-diff --file diff.patch          # Review a diff (live)
uv run fusion debug --error "TimeoutError: ..."      # Debug an error
uv run fusion decide --question "Redis or memcache?" # Architecture decision
uv run fusion plan --feature-file feature.md         # Implementation plan
uv run fusion eval-answer --question-file q.md --answer-file a.md
uv run fusion runs list                              # List recent runs
uv run fusion runs show RUN_ID                       # Show run details
uv run fusion version                                # Show version

# Offline flags
uv run fusion review-diff --file diff.patch --mock
uv run fusion run-mock --task debugging              # End-to-end mock pipeline
```

## Running evaluations on real tasks

### Offline dataset evals

Datasets live in `evals/datasets/` as JSONL files.

```bash
# Live eval on code review cases (costs API credits)
uv run python evals/runners/run_offline_eval.py --dataset code_review

# Save results for analysis
uv run python evals/runners/run_offline_eval.py \
  --dataset debugging \
  --output evals/results/debugging-$(date +%Y%m%d).jsonl

# Compare all task types quickly
uv run python evals/runners/compare_pipelines.py
```

Available datasets: `code_review`, `debugging`, `architecture`, `planning`.

### Evaluating Fusion on your own tasks

1. **Prepare input** — diff, error + logs, architecture question, or feature description.
2. **Run via CLI** and capture `run_id`:

   ```bash
   uv run fusion review-diff --file my-change.patch > result.json
   uv run fusion runs show RUN_ID
   ```

3. **Inspect scores** in the output:
   - `evals.final.overall_score` — hybrid quality score (0–1)
   - `evals.deterministic` — safety/completeness flags
   - `disagreement.disagreement_score` — panel disagreement
   - `routing.selected_panel` — which models ran
   - `confidence` — synthesis confidence

4. **Add custom cases** to `evals/datasets/*.jsonl`:

   ```json
   {"id": "my-case-1", "diff": "...", "context": "...", "expected_themes": ["auth", "sql"]}
   ```

## Comparing Fusion vs Opus / Claude Code team agents

Fusion and Claude Code agents work together. Fusion is a **multi-model reasoning panel** that can act like a cheaper coding model; Claude Code is the executor that reads, edits, and tests the repository. Compare “Claude Code + Opus” against “Claude Code + Fusion MCP + cheaper models” on the same tasks.

### Recommended comparison protocol

1. **Pick 5–10 real tasks** from your repo (diffs, bugs, architecture decisions).
2. **Baseline — Claude Code alone (Opus or Sonnet)**  
   Run the task with Claude Code only. Save the answer and note time, cost, and whether you had to iterate.
3. **Baseline — Claude Code team / subagents**  
   Run the same task with Claude Code’s team agents if available. Save outputs.
4. **Treatment — Claude Code + Fusion**  
   Call `fusion_ask` or the matching specialized Fusion MCP tool with the same context. Let Claude Code apply the answer and run tests.
5. **Score with Fusion eval**  

   ```bash
   uv run fusion eval-answer \
     --question-file task.md \
     --answer-file baseline-answer.md
   uv run fusion eval-answer \
     --question-file task.md \
     --answer-file fusion-answer.md
   ```

6. **Compare dimensions**

   | Dimension | What to measure |
   |-----------|-----------------|
   | Correctness | Did it catch the bug / right architecture? |
   | Groundedness | Unsupported claims? (`evals.deterministic`, `unsupported_claims`) |
   | Specificity | Actionable steps vs vague advice (`evals.llm_judge.specificity`) |
   | Safety | Secret leakage, dangerous commands flagged? |
   | Disagreement value | Did multi-model panel surface issues single model missed? |
   | Cost/latency | `total_cost_usd`, `total_latency_ms` in run record |
   | Iterations | How many back-and-forth turns to reach acceptable output? |

7. **When Fusion should win**
   - Security-sensitive code review (security + coding panel roles)
   - High-stakes architecture decisions (diverse model perspectives)
   - Debugging with ambiguous symptoms (competing hypotheses)
   - Answers you want scored before merging or deploying

8. **When single Opus/agent may win**
   - Small localized edits
   - Tasks where the panel context is too thin or repeated file exploration matters more than model diversity
   - Latency-sensitive loops where panel fan-out is too slow

### A/B workflow in Claude Code

```text
1. Run the task in Claude Code with Opus/native model and save the result
2. Run the same task in Claude Code, but call fusion_ask or a specialized Fusion tool for reasoning
3. Call fusion_compare_claude_runs with the original prompt and both outputs
4. Compare quality winner, cost winner, latency winner, and missed findings
```

Use `uv run fusion runs list` to compare cost and latency across runs.

CLI equivalent for saved outputs:

```bash
uv run fusion compare-claude-runs \
  --task-file task.md \
  --opus-file claude-opus-output.md \
  --fusion-file claude-fusion-output.md \
  --context-file verification.md \
  --opus-cost 0.42 \
  --fusion-cost 0.07 \
  --opus-latency-ms 90000 \
  --fusion-latency-ms 45000
```

## Security

Fusion is **side-effect free inside MCP** — it analyzes text you pass in and returns model-like answers. Claude Code remains the component that edits files and executes shell commands.

| Control | Behavior |
|---------|----------|
| Secret redaction | All input is scanned and redacted before external provider calls |
| Sanitized logging | Raw prompts are off by default (`FUSION_LOG_RAW_PROMPTS=false`) |
| Deterministic safety checks | Flags secret leakage, dangerous shell commands, unsupported file references |
| Judge skepticism | LLM judge outputs are self-evaluated; deterministic evals run even if judge fails |
| No OpenRouter | Direct provider adapters only — your keys stay in your environment |
| MCP boundary | MCP server handlers call pipelines only; no repo writes or shell execution |

Always verify Fusion recommendations against your codebase before applying changes.

## Development

```bash
uv sync --all-groups
uv run pytest -v
uv run ruff check src tests evals
uv run ruff format src tests
```

## Project Structure

```text
src/fusion/
  mcp_server/     MCP tool handlers and schemas
  orchestration/  Pipelines, fan-out, synthesis, prompts (real roles)
  providers/      Direct API adapters (anthropic, openai, google, ollama, lmstudio, mock)
  routing/        Classifier, registry, budget, policy, Router
  config/         default_models.yaml, routing_policies.yaml, env loading
  evals/          LLM judge + deterministic checks
  security/       Secret redaction
  storage/        SQLite run logging
  cli/            Typer CLI
plugin/           Claude Code plugin (skills, commands, MCP config)
evals/            Offline eval datasets and runners
tests/            pytest test suite (mock mode via conftest)
```

## License

MIT — see [LICENSE](LICENSE).
