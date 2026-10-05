# Configuration

Canonical reference for environment variables, YAML config files, routing, budgets, fan-out and
refinement. Cost and pricing methodology lives in [COSTS.md](COSTS.md).

## Files

| File | Purpose |
|------|---------|
| `.env` | Provider keys and runtime toggles. Loaded automatically by the CLI and the MCP server. |
| `src/fusion/config/catalog.yaml` | Model catalog: aliases, provider model IDs, capabilities, tiers, enable flags and verified prices. |
| `src/fusion/config/routing_policies.yaml` | Task routing, per-budget panels, fan-out and refinement settings. |
| `src/fusion/config/baseline.yaml` | Frontier baseline(s), as catalog aliases, that Fusion's cost is compared against. |

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
| `FUSION_DB_PATH` | SQLite database path | `./fusion_runs.db` |
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
the `local_only` budget is requested but no local models are configured, Fusion falls back to cloud
models with a warning.

```bash
export OLLAMA_ENABLED=true
ollama serve
ollama pull llama3.2
# then set `ollama-llama` to enabled: true in catalog.yaml
```

## Routing and budgets

Every tool accepts an optional `budget`: `low`, `medium`, `high` or `local_only`. Per-task panels,
the judge and the synthesizer are defined in `routing_policies.yaml`; per-budget overrides sit under
each policy's `budgets:` key. Model IDs, capabilities, prices and enable flags are in `catalog.yaml`.

| Budget | Behaviour |
|--------|-----------|
| `low` | One cheap model, no refinement |
| `medium` | Cheap three-model panel, no refinement |
| `high` | Cheap panel plus the refinement round |
| `local_only` | Only Ollama / LM Studio models |

Default panel at `medium` budget:

| Role | Models |
|------|--------|
| Code review panel | Claude Haiku 4.5, GPT-6 Luna (security role), Gemini 3.8 Flash |
| Debug panel | Claude Haiku 4.5, GPT-6 Luna, Gemini 3.8 Flash |
| Judge | Gemini 3.8 Flash (JSON scoring) |
| Synthesizer | Claude Sonnet 5.5 |

The panel is intentionally cheap; the synthesizer is the strongest model in the loop because
mixture-of-agents quality depends most on the final aggregation step. High-risk code reviews
automatically add Claude Sonnet 5.5 to the panel. Panel members receive real role prompts defined in
`src/fusion/orchestration/prompts.py`.

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
```

Fusion preserves partial panel results. A failed or timed-out model produces a warning and a usage
record, but the run continues when quorum is met. If quorum is not met, Fusion returns a structured
diagnostic instead of pretending synthesis succeeded. Internals: [ARCHITECTURE.md](ARCHITECTURE.md#fanout).

## Refinement (mixture-of-agents)

At the budgets listed in `enabled_budgets`, each surviving panel model sees the other models'
answers anonymized as "Response A/B/C" plus its own, critiques them, and returns a revised answer
before synthesis. A model whose refinement call fails keeps its round-1 answer.

```yaml
refinement:
  enabled_budgets: [high]
  per_model_timeout_seconds: 45
  global_timeout_seconds: 60
  min_panel_size: 2
  max_rounds: 1
```

## Pricing and baseline

Prices (which live in the catalog), their provenance and the baseline are described in [COSTS.md](COSTS.md). Run
`uv run fusion config validate` after editing the catalog, routing or baseline YAML.
