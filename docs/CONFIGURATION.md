# Configuration

Canonical reference for environment variables, YAML config files, strategies, budgets, fan-out and
refinement. Cost and pricing methodology lives in [COSTS.md](COSTS.md).

## Layers and locations

Settings come from five layers; a later layer overrides an earlier one:

1. **Packaged defaults**: `catalog.yaml`, `routing_policies.yaml`, `strategies.yaml` and
   `baseline.yaml` inside the package (`src/fusion/config/`). Do not edit these in an installed copy.
2. **User config**: `config.yaml` in the platform config directory (`fusion config paths` prints it;
   `fusion init` creates a commented starter).
3. **Project config**: `.fusion/config.yaml` in the working directory (or `FUSION_PROJECT_DIR`).
4. **Environment variables** named `FUSION__<SECTION>__<KEY>`, for example
   `FUSION__FANOUT__MAX_CONCURRENCY=2`. Values are parsed as YAML (`4`, `false`, `[high]`).
5. **`fusion --set section.key=value`** (repeatable, placed before the command).

User and project files may set these top-level sections: `models`, `provider_limits`, `policies`,
`budgets`, `fanout`, `refinement`, `strategies`, `budget_strategies` and `baselines`. A misspelled section is an error with a
suggestion. Mappings merge key by key, so `models: {gpt-luna: {max_tokens: 1234}}` changes one field
and keeps the rest; lists (`baselines`, a strategy's `members`) and single values are replaced.

```bash
uv run fusion config show                    # which layers exist and what each one sets
uv run fusion config show --resolved         # every effective value with the layer that set it
uv run fusion config show --resolved --filter fanout --json
uv run fusion config paths                   # config, database and legacy locations
```

Validation errors name the key, the offending value, the layer that set it and the rule that failed.

| File | Purpose |
|------|---------|
| `.env` | Provider keys and runtime toggles. Loaded automatically by the CLI and the MCP server. |
| `src/fusion/config/catalog.yaml` | Model catalog: aliases, provider model IDs, capabilities, tiers, enable flags and verified prices. |
| `src/fusion/config/routing_policies.yaml` | Per-task judge model and context threshold; fan-out and refinement settings. |
| `src/fusion/config/strategies.yaml` | Strategies (which models answer, rounds, aggregation, judging) and the budget-to-strategy table. |
| `src/fusion/config/baseline.yaml` | Frontier baseline(s), as catalog aliases, that Fusion's cost is compared against. |

## Run database

Runs are stored in `runs.db` in the platform data directory, not in the working directory of the
process that started Fusion. Override the location with `FUSION_DB_PATH` or `--db-path`. The
database uses WAL mode so the MCP server and the CLI can share it.

Upgrading from v0.1.0, which wrote `./fusion_runs.db`: that file is never read or moved
automatically. Copy its history over once with `fusion init --import-legacy` (the original stays
in place; the import refuses to merge into a database that already has runs).

Validate without calling any provider:

```bash
uv run fusion config validate
uv run fusion config validate --strict
```

Strict mode fails when an enabled cloud provider is missing its API-key variable. Non-strict mode
reports it as a warning so mock and local development stay easy.

## Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key | — |
| `OPENAI_API_KEY` | OpenAI API key | — |
| `GOOGLE_API_KEY` | Google API key | — |
| `OLLAMA_ENABLED` | Enable the Ollama provider | `false` |
| `OLLAMA_BASE_URL` | Ollama endpoint | `http://localhost:11434` |
| `LMSTUDIO_ENABLED` | Enable the LM Studio provider | `false` |
| `LMSTUDIO_BASE_URL` | LM Studio endpoint | `http://localhost:1234/v1` |
| `FUSION_DB_PATH` | SQLite database path | `runs.db` in the user data directory |
| `FUSION_CONFIG_DIR` | Directory holding the user `config.yaml` | platform config dir |
| `FUSION_DATA_DIR` | Directory holding `runs.db` | platform data dir |
| `FUSION_PROJECT_DIR` | Directory whose `.fusion/config.yaml` is the project layer | working directory |
| `FUSION__<SECTION>__<KEY>` | Override one config key (see [Layers and locations](#layers-and-locations)) | unset |
| `FUSION_SHADOW_MODE` | Shadow A/B against the real baseline: `off`, `sampled`, `always` | `off` |
| `FUSION_SHADOW_SAMPLE_RATE` | Fraction of runs shadowed in `sampled` mode | `0.2` |
| `FUSION_LOG_RAW_PROMPTS` | Log unsanitized prompts (dangerous) | `false` |
| `FUSION_DEFAULT_PROVIDER` | Set to `mock` for offline mode | unset (live) |

How the shadow A/B works and how to read its results: [BENCHMARKING.md](BENCHMARKING.md#shadow-ab-live-measurement).

## Providers

| Provider | Variables | Default |
|----------|-----------|---------|
| Anthropic | `ANTHROPIC_API_KEY` | Enabled when the key is set |
| OpenAI | `OPENAI_API_KEY` | Enabled when the key is set |
| Google | `GOOGLE_API_KEY` | Enabled when the key is set |
| Ollama | `OLLAMA_ENABLED=true`, `OLLAMA_BASE_URL` | Optional, off by default |
| LM Studio | `LMSTUDIO_ENABLED=true`, `LMSTUDIO_BASE_URL` | Optional, off by default |
| Mock | `FUSION_DEFAULT_PROVIDER=mock` or `--mock` | Tests and offline development only |

Ollama and LM Studio never block cloud usage. They are registered only when explicitly enabled. If
the `local_only` budget (strategy `panel-local`) is requested but no local models are enabled, Fusion
falls back to cloud models with a warning.

```bash
export OLLAMA_ENABLED=true
ollama serve
ollama pull llama3.2
# then set `ollama-llama` to enabled: true in catalog.yaml
```

### Rate limits and reasoning effort

Three catalog settings tune provider calls (all in `src/fusion/config/catalog.yaml`):

| Setting | Where | Effect |
|---------|-------|--------|
| `provider_limits.<provider>.max_concurrent` | top level | Most in-flight requests to that provider, shared by every call (panel, refinement, judge). |
| `provider_limits.<provider>.rpm` | top level | Most request starts in any 60 s window. Omit either key for "unlimited". Set both to your account's real quota. |
| `default_reasoning_effort` | per model | Effort sent when a request does not choose one (`none`..`max`; Gemini maps to `low`/`medium`/`high`). Panel models default to `low`; the baseline and synthesizer keep provider defaults so comparisons stay fair. |
| `persona` | per model | Panel persona prompt (for example `security_reviewer`). Without one a model answers with the task's own system prompt. |
| `max_tokens` | per model | Output cap Fusion requests. Thinking tokens count against it, so reasoning models use 16384. |

## Strategies and budgets

A **strategy** says who answers a task and how the answers are combined. Every tool accepts an
optional `strategy` (a name from `strategies.yaml`); `fusion strategies list` prints them. Without
one, the legacy `budget` argument picks the strategy through `budget_strategies`, and the response's
`routing.strategy` reports which one ran. Switching strategy is a config or argument change, never a
code change.

```yaml
strategies:
  my-panel:
    kind: panel                  # solo | panel
    members:                     # catalog aliases; solo has exactly one
      - model: claude-haiku
      - model: gpt-luna
        role: security_reviewer  # "auto" (default) = the catalog persona, else the task prompt
        temperature: 0.2         # optional per member
        reasoning_effort: low    # optional per member
    rounds: 2                    # 1 = answer once; 2 = plus one peer-refinement round
    aggregator: llm              # llm | digest
    aggregator_model: claude-sonnet   # omit for the catalog's synthesizer role
    judge: off                   # off | light | full
    judge_feeds_synthesis: false # true = the synthesizer reads the judge's scores (and waits)
    max_cost_usd: 0.05           # optional; going over adds a warning
    max_latency_s: 60            # optional; going over adds a warning
budget_strategies:
  medium: my-panel
```

| Strategy | Runs | Calls per task |
|----------|------|----------------|
| `solo-frontier` | Claude Opus 5.5 | 1 |
| `solo-sol` | GPT-6.1 Sol | 1 |
| `solo-cheap` | Claude Haiku 4.5 | 1 |
| `solo-luna` | GPT-6 Luna | 1 |
| `panel-cheap` (default) | Haiku 4.5 + GPT-6 Luna + Gemini 3.8 Flash, merged by Haiku 4.5 | 3 + 1 |
| `panel-cheap-strong-synth` | the same panel, merged by Claude Sonnet 5.5 | 3 + 1 |
| `panel-refine` | the same panel plus one refinement round, merged by Haiku 4.5 | 3 + 3 + 1 |
| `panel-digest` | the same panel, no synthesis: the answers come back for Claude Code to merge | 3 |
| `panel-local` | Ollama and LM Studio models; falls back to the cloud panel with a warning when none are enabled | 2 + 1 |

How the fields behave:

- **`solo`** returns the model's answer as is (one call, no synthesis). Benchmark baselines are solo
  strategies, so a baseline and Fusion share one code path and one cost ledger.
- **`rounds`** counts answer rounds: `rounds: 3` is the panel plus two refinement rounds. In a
  refinement round each member sees the other members' anonymized answers and revises its own; a
  member whose call fails keeps its previous answer. Timeouts and the minimum panel size are under
  [Refinement](#refinement-mixture-of-agents).
- **`aggregator: llm`** makes one synthesizer call; **`digest`** makes none and returns every answer
  plus the disagreement summary. `vote` and `best_of` are reserved names and are rejected for now.
- **`judge`**: `off` runs only the deterministic checks and makes no judge call (the default, so a
  run costs only its panel and aggregator). `light` has the judge model score each answer.
  `full` does the same and also records a check of the judge's own output under
  `evals.judge_quality`. Which model judges is the task's `judge_model` in `routing_policies.yaml`.
- **`judge_feeds_synthesis`** (default `false`): by default the judge and synthesis run at the same
  time, since the synthesizer does not read the judge's scores. Set it to `true` for the synthesizer
  to receive them; synthesis then waits for the judge. It needs `judge: light` or `full` and an
  `llm` aggregator.
- A model may appear only once in a strategy. `kind: cascade` is reserved and rejected for now.
- `max_cost_usd` and `max_latency_s` are checked after the run and add a warning; they do not stop
  a run.

Budgets are aliases kept for compatibility:

| Budget | Strategy |
|--------|----------|
| `low` | `solo-cheap` |
| `medium` | `panel-cheap` |
| `high` | `panel-refine` |
| `local_only` | `panel-local` |

Override a strategy field the same way as any setting, for example
`FUSION__STRATEGIES__PANEL-CHEAP__AGGREGATOR_MODEL=claude-sonnet` or
`fusion --set strategies.panel-cheap.judge=light review-diff ...`. `fusion config validate` checks
that every strategy names catalog models.

The panel is intentionally cheap; a stronger aggregator (`panel-cheap-strong-synth`) is the first
lever when quality matters more than cost. Panel members receive the role prompts in
`src/fusion/orchestration/prompts.py`.

### Upgrading from v0.1.0

`policies.<task>.panel_models`, `max_panel_size`, `high_risk_*`, `budgets` and `synthesizer_model`, and
`refinement.enabled_budgets` and `max_rounds`, are gone; a config that still sets them fails with a
message naming the key. Move the panel into a strategy and set `rounds` for refinement. The
per-task choices went away with them: code review no longer swaps in the security-focused GPT-6 Luna
(`gpt-luna-security` remains in the catalog; give a member `role: security_reviewer` to get the same
prompt for every task) and no longer widens the panel for high-risk diffs.

## Modes

A run is either `real` (the default; serving Claude Code) or `benchmark` (measured in a study). The
mode is an argument of `Pipeline.run(ctx, mode=...)`, not a global.

| | `real` | `benchmark` |
|--|--------|-------------|
| Secret redaction | on | on |
| Judge | the strategy's `judge` (default `off`) | the strategy's `judge` |
| Sampling | provider defaults | temperature `0` and seed `0` unless a member sets its own (seeds reach OpenAI, Google and Ollama; Anthropic has none) |
| Prompts over a model's context window | trimmed, with a warning | sent as is |
| Shadow A/B | per `FUSION_SHADOW_MODE` and `shadow_baseline` | never |
| Lifetime-stats footer | appended at `detail: full` | omitted |
| Stored ledger | full | full |

The benchmark runner that drives this mode and its provider-response cache arrive with the benchmark
framework; see [BENCHMARKING.md](BENCHMARKING.md).

## Fan-out

Panel calls run concurrently inside one blocking MCP request. Fan-out limits are in
`routing_policies.yaml`:

```yaml
fanout:
  max_concurrency: 6
  per_model_timeout_seconds: 45
  global_timeout_seconds: 60
  min_successful_responses: 2
  cancel_on_global_timeout: true
  allow_partial_results: true
  early_return: {quorum: 2, grace_ms: 1500}   # optional, off by default
  hedge_after_ms: 8000                        # optional, off by default
```

`max_concurrency` is per provider: one provider's calls queue behind each other, other providers'
do not. Two optional settings trade a little quality or cost for latency:

- `early_return`: once `quorum` members have answered (and at least `min_successful_responses`),
  wait `grace_ms` more for the rest, then cancel them and carry on. The run warns which models were
  dropped. A cancelled call may still be billed, so its cost is recorded as unknown.
- `hedge_after_ms`: when a member has not answered after this long, ask the next enabled model with
  the `panel` role that is not already in the panel. Whichever of the two answers first is used,
  attributed to the model that actually answered, and the other is cancelled. With no spare model
  the run says so and keeps waiting.

Fusion preserves partial panel results. A failed or timed-out model produces a warning and a usage
record, but the run continues when quorum is met. If quorum is not met, Fusion returns a structured
diagnostic instead of pretending synthesis succeeded. Internals: [ARCHITECTURE.md](ARCHITECTURE.md#concurrency-and-latency).

## Refinement (mixture-of-agents)

Strategies with `rounds` above 1 add refinement rounds: each surviving panel model sees the other
models' answers anonymized as "Response A/B/C" plus its own, critiques them, and returns a revised
answer before synthesis. A model whose refinement call fails keeps its previous answer. The
controls below apply to every refinement round; how many rounds run is the strategy's `rounds`.

```yaml
refinement:
  per_model_timeout_seconds: 45
  global_timeout_seconds: 60
  min_panel_size: 2          # fewer answers than this and refinement is skipped with a warning
  skip_above_agreement: 0.8  # skip a round when the panel already agrees this much; null = always refine
```

Every revision of a round starts together once all round-1 answers are in. The agreement is the
score from [claim clustering](ARCHITECTURE.md#claims-and-agreement); a skipped round adds a warning.

## Pricing and baseline

Prices (which live in the catalog), their provenance and the baseline are described in [COSTS.md](COSTS.md). Run
`uv run fusion config validate` after editing the catalog, routing or baseline YAML.
