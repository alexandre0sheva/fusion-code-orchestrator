# Fusion Code Orchestrator

Multi-model orchestration and evaluation engine for coding workflows. Exposed as a **Python MCP server** for Claude Code and Cursor, with direct provider adapters (no OpenRouter).

Claude Code remains the main coding agent. Fusion gives it external tools for architecture decisions, code review, debugging hypotheses, implementation planning, and model answer evaluation — using **real multi-model panels** (Anthropic, OpenAI, Google) by default.

## Features

- **5 MCP tools**: code review, debug, architecture, planning, answer evaluation
- **Multi-model fan-out**: concurrent panel calls with disagreement analysis
- **Hybrid evaluation**: LLM-as-judge + deterministic safety/completeness checks
- **Direct provider adapters**: Anthropic, OpenAI, Google (Ollama/LM Studio optional)
- **YAML model registry** with cost/quality/latency tiers and routing policies
- **Budget-aware routing**: low / medium / high / local_only
- **Secret redaction**: API keys, tokens, passwords redacted before external calls
- **SQLite logging**: full run traces with costs and latency
- **Read-only/advisory**: no file edits or shell execution from the orchestrator

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended)
- At least one cloud API key: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and/or `GOOGLE_API_KEY`

### Install

```bash
git clone https://github.com/your-org/fusion-code-orchestrator.git
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
```

### Run tests (offline, no API keys)

```bash
uv run pytest
uv run ruff check src tests
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

## Agent mode and Opus vs Fusion benchmarks

Fusion can run as a **coding agent** (read/write files, run shell) when enabled:

```bash
# .env
FUSION_AGENT_MODE=true
FUSION_WORKSPACE_ROOT=/absolute/path/to/your/project
```

Agent operations are restricted to `FUSION_WORKSPACE_ROOT`. Secrets in commands are blocked.

### One-shot A/B: implement with Opus vs Fusion

From **Claude Code** (single MCP call returns everything):

```text
Call fusion_compare_implement with:
- task: "Add a FUSION_AGENT_MODE section to README"
- workspace_root: "/Users/alexander/apps/app-coder/fusion-code-orchestrator"
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

Fusion returns structured JSON with findings, confidence, hybrid evals, routing metadata, and cost/latency. **You** apply changes — Fusion is advisory only.

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
| `fusion_review_diff` | Multi-model code review with synthesis |
| `fusion_debug_error` | Debug analysis with fix recommendations |
| `fusion_decide_architecture` | Architecture decision support |
| `fusion_plan_feature` | Implementation planning |
| `fusion_eval_answer` | Answer quality evaluation |

Each tool accepts an optional `budget` field: `low`, `medium`, `high`, or `local_only`.

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

Fusion and Claude Code agents solve different problems. Fusion is a **multi-model advisory panel**; Claude Code agents are **single-agent executors**. Compare them on tasks where panel diversity matters.

### Recommended comparison protocol

1. **Pick 5–10 real tasks** from your repo (diffs, bugs, architecture decisions).
2. **Baseline — Claude Code alone (Opus or Sonnet)**  
   Run the task with Claude Code only. Save the answer and note time, cost, and whether you had to iterate.
3. **Baseline — Claude Code team / subagents**  
   Run the same task with Claude Code’s team agents if available. Save outputs.
4. **Treatment — Fusion advisory**  
   Call the matching Fusion MCP tool with the same context. Apply recommendations yourself in Claude Code.
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
   - Tasks requiring deep repo exploration and file edits (Fusion is read-only)
   - Latency-sensitive loops where panel fan-out is too slow

### A/B workflow in Claude Code

```text
1. Paste diff → ask Claude Code (Opus) for review → save as answer-a.md
2. Call fusion_review_diff with same diff → save synthesis as answer-b.md
3. Run fusion_eval_answer on both against the same rubric
4. Compare scores + your own judgment on missed findings
```

Use `uv run fusion runs list` to compare cost and latency across runs.

## Security

Fusion is **read-only/advisory** — it analyzes text you pass in; it never edits files or executes shell commands.

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
