# Architecture

Fusion Code Orchestrator is a Python MCP server that gives Claude Code model-like
multi-model workflows for coding tasks. It calls providers directly through adapters.

## Pipeline flow

A run is a list of **stages** that read and write one `RunState` (`src/fusion/orchestration/`):

```text
Claude Code -> MCP tool -> specialized pipeline -> BasePipeline.run(ctx)
   RedactStage       redact secrets, open the run record
   RouteStage        classify, pick panel/judge/synthesizer, fall back by catalog role
   ContextEvalStage  score the context            -- halts: "insufficient context"
   PanelStage        concurrent fan-out           -- halts: quorum not met
   RefineStage       optional peer-review round (high budget)
   JudgeStage        LLM judge + deterministic checks per answer
   AggregateStage    disagreement analysis + synthesis
   FinalEvalStage    final eval, structured output, budget warnings
   ShadowStage       optional blind A/B against the real baseline
   PersistStage      build the result from the ledger, store the run (always runs)
```

A halted run skips straight to `PersistStage`, which still stores a diagnostic result. Stages hold
no state of their own, so each can be unit-tested with a prepared `RunState`. The modules are
`context` (inputs, shared dependencies, `RunState`), `stages`, `ledger`, `result`, `output`
(display text, usage and persisted views), `pipeline` (the runner), `specialized` (the six
task pipelines that map results to tool outputs) and `factory` (`build_pipelines(Settings,
providers)`). `pipelines.py` only re-exports; `create_pipeline(s)` are deprecated aliases.

### Cost ledger

Every LLM call goes through one `CallGateway` (`ledger.py`), which times it, prices it from the
catalog and appends a `CallRecord` to the run's `RunLedger`: stage (`panel`, `refine`, `judge`,
`synthesis`, `shadow_baseline`, `shadow_judge`, ...), model, tokens (input, output, cached,
reasoning), cost and whether it is known, latency, start offset, retries and speed metrics. Cost,
token, latency and per-stage totals are all derived from the ledger, so judge and eval calls are
part of the reported Fusion cost; shadow stages are excluded because they are measurement
overhead. A call that fails or times out may still have been billed, so its cost is recorded as
unknown and the run's total is flagged unknown. `RunLedger.task_metrics()` gives the per-task
numbers the benchmark mode will store. The full ledger is saved in each run's output.

The gateway also keeps prompts inside the model's context window: if a prompt exceeds the catalog
`context_window` it is trimmed in the middle and a warning naming the model is added to the run.
Models with no declared window are never trimmed.

### Modes

Offline (mock) and live mode differ in configuration, not in code paths. `factory.build_deps` picks
the mode once (`Settings.use_mock`, or `FUSION_DEFAULT_PROVIDER=mock`): live mode builds a registry
of real models and the packaged routing policies; offline mode builds a registry of mock models and
policies pointing at them (`mock_routing_policies`). Router, stages and fallbacks never ask whether
they are under test; role fallbacks (`panel`, `judge`, `synthesizer`) come from the catalog roles of
whichever registry is in use. A panel model's persona prompt comes from its catalog `persona`
field; models without one answer with the task's own system prompt.

Panel members receive role prompts defined in `src/fusion/orchestration/prompts.py`.

## Components

### MCP server

`src/fusion/mcp_server/server.py` registers the public tools:

- `fusion_review_diff`
- `fusion_ask`
- `fusion_debug_error`
- `fusion_decide_architecture`
- `fusion_plan_feature`
- `fusion_eval_answer`
- `fusion_compare_claude_runs`
- `fusion_stats`

`src/fusion/mcp_server/tools.py` converts MCP input schemas into orchestration pipeline
inputs and returns Pydantic output models as JSON dictionaries.

### Providers

Providers implement `ModelProvider` in `src/fusion/providers/base.py`. The three cloud adapters
and the two local ones share `HttpProvider` (`providers/http_utils.py`), which owns:

- **One pooled `httpx.AsyncClient` per provider**, created lazily (HTTP/2 for cloud providers) and
  re-created if the event loop changes. `aclose()` releases it; `FusionTools.aclose()` and the MCP
  server lifespan close every provider on shutdown, and the CLI closes them after each command.
- **Retries**: only transient failures (429, 408, 5xx, timeouts, connection errors) are retried, with
  full-jitter exponential backoff. `Retry-After` is honoured, and one longer than 60 s is returned
  as an error instead of waited for. 4xx errors are never retried.
- **A typed error taxonomy** surfaced as `ModelResponse.error_type`: `RateLimit`, `Auth`, `Timeout`,
  `BadRequest`, `Server`, `Connection`. Error text is scrubbed of API keys; keys travel in headers,
  never in URLs.
- **Rate limiting** through `ProviderLimiter` (`providers/limits.py`): a semaphore for in-flight
  requests plus a sliding one-minute request window, shared by all calls to a provider. Limits are
  configured in the catalog ([CONFIGURATION.md](CONFIGURATION.md#rate-limits-and-reasoning-effort)).

Per-model behaviour comes from the catalog entry (`supports_sampling_params`,
`supports_json_schema`, `supports_reasoning_effort`, `supports_vision`), not from model-name regexes.
`ModelRequest` carries the optional features; an adapter maps each to its provider's native form:

| `ModelRequest` field | Anthropic | OpenAI / LM Studio | Gemini |
|----------------------|-----------|--------------------|--------|
| `response_schema` | `output_config.format` (JSON Schema) | `response_format: json_schema` | `responseSchema` (refs inlined) |
| `reasoning_effort` | `output_config.effort` | `reasoning_effort` | `thinkingConfig.thinkingLevel` |
| `cache_prefix` | `cache_control` on the system prompt and every message before the last | automatic | automatic |
| `images` | base64 image blocks | `image_url` data URIs | `inlineData` parts |
| `stream` | SSE `messages` stream | SSE with `include_usage` | `streamGenerateContent?alt=sse` |

When a model does not support native structured output, the schema is appended to the system prompt
and the JSON is scraped from the reply. Forced tool choice is not used for structured output because
Claude 5.x rejects it. `thinking_budget_tokens` is only valid for models without adaptive thinking
and is refused locally otherwise.

Adapters normalize usage so costs are computed from one definition: `input_tokens` is the total
including cached reads and cache writes, `output_tokens` includes reasoning tokens. Anthropic reports
cache reads and writes outside `input_tokens` and Gemini reports thinking tokens outside
`candidatesTokenCount`, so the adapters add them back; OpenAI already includes both. Cache writes are
priced at the catalog's `cache_write_per_1m`.

`ModelResponse` also carries speed metrics measured at the source: `total_tokens_per_s` (output
tokens over wall latency), and for streamed calls `ttft_ms` (first non-empty text delta) and
`decode_tokens_per_s` (output tokens over latency after the first token). Token counts always come
from provider usage, never from character counts. Local providers do not stream.

Cloud providers are Anthropic, OpenAI, and Google. Ollama and LM Studio support local-only
routes. `MockProvider` supports deterministic offline tests and development.

### Router

`src/fusion/routing/policy.py` classifies task type, complexity, and risk, then selects:

- panel models;
- judge model;
- synthesizer model;
- budget tier and routing warnings.

Model metadata and prices live in the catalog, `src/fusion/config/catalog.yaml`. Task policies and fanout
settings live in `src/fusion/config/routing_policies.yaml`.

### Fanout

`src/fusion/orchestration/fanout.py` starts panel calls concurrently with `asyncio`.

It tracks:

- per-model timeout;
- global panel timeout;
- concurrency limit;
- minimum successful responses;
- partial results;
- structured failure status;
- panel wall latency, max model latency, and summed model-call latency.

Config keys and failure semantics are in [CONFIGURATION.md](CONFIGURATION.md#fan-out). A panel call
that is still pending when the global timeout hits is attributed to its own model. Synthesis runs
after fanout and disagreement analysis. The MCP request is still a normal blocking request from
Claude Code's perspective.

### Refinement (mixture-of-agents)

`src/fusion/orchestration/refine.py` runs an optional second round after fanout when
the routing config enables it for the effective budget (`refinement.enabled_budgets`,
default `[high]`). Each successful panel model receives the peer answers anonymized as
"Response A/B/C" plus its own answer and returns a revised answer. Failures keep the
round-1 answer. Refined answers feed the judge, disagreement analysis, and synthesis;
each call is traced as `refine:{model}` with full usage and cost.

### Shadow baseline A/B

`src/fusion/benchmark/shadow.py` optionally calls the configured baseline model
(Opus 5.5 by default) on the same sanitized task after synthesis, then asks the judge model for a
blind pairwise verdict (answers presented in randomized order, unlabeled). Trigger via
`FUSION_SHADOW_MODE` (`off` | `sampled` | `always`, with `FUSION_SHADOW_SAMPLE_RATE`)
or a per-call `shadow_baseline` flag which overrides the env in both directions.

Results are stored in the `shadow_comparisons` table (winner, blind scores, actual
baseline cost and latency). When a shadow ran, the run's `cost_comparison` reports the
actual baseline cost instead of the token-volume estimate. Shadow costs are measurement
overhead and are never counted as Fusion cost. All shadow failures degrade to warnings.

### Stats

`RunStore.get_stats()` aggregates cumulative totals: run counts, Fusion spend, baseline
estimates, per-task-type breakdown, and shadow win/tie/loss counts. Rendering lives in
`src/fusion/telemetry/stats_format.py`, surfaced through the `fusion stats` CLI command,
the `fusion_stats` MCP tool, and a one-line lifetime footer on every run's
`display_markdown`.

### Evals

`src/fusion/evals/engine.py` coordinates hybrid evals:

- deterministic checks always run;
- LLM judge runs when a configured judge model is available;
- heuristic fallback runs when judge calls fail;
- final aggregate confidence combines context sufficiency, consensus, answer quality,
  final quality, provider success rate, unsupported-claim penalty, and residual risk.

Eval data affects warnings, confidence, and the final MCP output.

### Synthesis

Panel outputs are scored and checked for disagreement. The synthesizer prompt receives:

- original task;
- panel responses;
- disagreement analysis;
- requested JSON schema.

Raw panel outputs are not included in MCP responses unless `include_raw_outputs=true`.

### Telemetry

`src/fusion/telemetry/cost.py` owns usage and baseline comparison schemas:

- `ModelUsage`
- `UsageSummary`
- `CostComparison`
- `PricingRegistry`

Prices come from the model catalog (`src/fusion/config/catalog.yaml`, date-aware price schedules
with provenance); baseline comparison is loaded from `src/fusion/config/baseline.yaml`. See
[COSTS.md](COSTS.md).

### Storage

`src/fusion/storage/run_store.py` stores each run in SQLite (location and upgrade notes:
[CONFIGURATION.md](CONFIGURATION.md#run-database)). A `RunStore` owns one WAL-mode connection guarded
by a lock; async code uses its `a*` methods, which run the query in a worker thread. Schema
migrations apply atomically (`BEGIN IMMEDIATE`) so concurrent first opens are safe. Each run stores:

- run ID and timestamp;
- original and sanitized inputs;
- routing;
- trace;
- panel, synthesis, and final outputs;
- evals;
- usage summary;
- cost comparison;
- warnings and errors.

CLI inspection commands:

```bash
uv run fusion runs list
uv run fusion runs show RUN_ID
uv run fusion runs costs
uv run fusion runs compare-baseline RUN_ID
uv run fusion runs export --format jsonl
```

## Safety boundaries

The orchestration MCP tools are side-effect free inside MCP: they do not execute shell
commands in user repositories or edit files themselves. That boundary should not block
Claude Code. Claude Code can use Fusion's answer like a cheaper model response, then edit,
run tests, and continue the normal coding workflow.
