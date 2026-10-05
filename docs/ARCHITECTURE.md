# Architecture

Fusion Code Orchestrator is a Python MCP server that gives Claude Code model-like
multi-model workflows for coding tasks. It calls providers directly through adapters.

## Pipeline flow

A run is a list of **stages** that read and write one `RunState` (`src/fusion/orchestration/`):

```text
Claude Code -> MCP tool -> specialized pipeline -> BasePipeline.run(ctx)
   RedactStage       redact secrets, open the run record
   RouteStage        apply the strategy: panel/aggregator/judge models, fall back by catalog role
   ContextEvalStage  score the context            -- halts: "insufficient context"
   BudgetStage       forecast the cost against max_cost_usd; shift the strategy down if it is over
                                                  -- halts: cap below the cheapest option
   ShadowStartStage  start the shadow baseline call now, if a shadow run is wanted
   PanelStage        concurrent fan-out           -- halts: quorum not met
                     (a cascade asks its cheapest members first and may stop there)
   RefineStage       the strategy's extra peer-review rounds (rounds - 1; none for rounds: 1),
                     skipped when the panel already agrees or a cascade already stopped
   ClaimsStage       read answers as claims, cluster them across models, measure agreement
   ConcurrentStages  at the same time:
     JudgeStage        safety checks and claim-derived scores per answer; LLM judge if on
     AggregateStage    final answer (solo: the answer itself; llm: one synthesizer call;
                       digest, vote, best_of: built from the claims, no call)
   FinalEvalStage    final eval, structured output, budget warnings
   ShadowStage       collect the baseline answer and judge it blind against Fusion's
   PersistStage      build the result from the ledger, store the run (always runs)
```

The stage list is the same for every strategy; each stage reads the strategy from `RunState` and
skips what it does not call for (a solo run has no refinement round and no synthesis call). A
halted run skips straight to `PersistStage`, which still stores a diagnostic result. Stages hold
no state of their own, so each can be unit-tested with a prepared `RunState`. The modules are
`context` (inputs, shared dependencies, `RunState`), `strategy` (strategies and run modes),
`stages`, `ledger`, `result`, `output`
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

### Strategies and run modes

A **strategy** (`strategy.py`, packaged in `config/strategies.yaml`) is data: `kind` (`solo`,
`panel` or `cascade`), `members` (catalog aliases, each with an optional role, temperature and reasoning effort),
`rounds`, `aggregator`, `judge` and optional cost/latency caps. `RunState.start` resolves it once
per run, from `PipelineContext.strategy` or else the legacy `budget` through `budget_strategies`, so
an unknown name fails before a run is recorded. The router turns the strategy into the models to
call, and the stages do the rest. Benchmark baselines are `solo` strategies, so a baseline and Fusion
share one code path and one ledger. The fields are documented in
[CONFIGURATION.md](CONFIGURATION.md#strategies-and-budgets).

A run also has a **mode** (`Mode.REAL` or `Mode.BENCHMARK`), passed as `Pipeline.run(ctx, mode=...)`
and kept on `RunState` and `PipelineResult`. `MODE_SETTINGS` holds what a mode changes: whether the
lifetime footer and shadow A/B are allowed, whether prompts are trimmed to the context window, and a
fixed temperature and seed. `CallGateway` applies the last two for every call of the run (a request
or member that sets its own temperature wins). The table is in
[CONFIGURATION.md](CONFIGURATION.md#modes).

### Offline and live

Offline (mock) and live mode differ in configuration, not in code paths. `factory.build_deps` picks
the mode once (`Settings.use_mock`, or `FUSION_DEFAULT_PROVIDER=mock`): live mode builds a registry
of real models and the packaged strategies; offline mode builds a registry of mock models and
strategies of the same shapes running on them (`mock_strategy_book`). Router, stages and fallbacks never ask whether
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

`src/fusion/routing/policy.py` classifies task type, complexity, and risk, then turns the run's
strategy into:

- panel models (the strategy's enabled members; if none is enabled, the catalog's panel-role models
  with a warning);
- the judge model (the task policy's `judge_model`, else a JSON-capable model with the judge role);
- the aggregator model (an `llm` aggregator only; empty for solo, digest, vote and best_of);
- the resolved strategy name, an estimated cost tier and routing warnings.

`RoutingDecision.strategy` reports the strategy that ran. Model metadata and prices live in the
catalog, `src/fusion/config/catalog.yaml`; per-task judge and context thresholds and fan-out settings
in `src/fusion/config/routing_policies.yaml`; strategies in `src/fusion/config/strategies.yaml`.

### Concurrency and latency

A run's wall time is its critical path, not the sum of its calls. What overlaps:

```text
t0 ─┬─ panel member A ──────────┐
    ├─ panel member B ────┐     │  (every member starts at once; stragglers may be cut off or hedged)
    ├─ panel member C ───────┐  │
    └─ shadow baseline ─────────────────────────┐   (started at t0, collected at the end)
                             └──┴─ refine A/B/C ─┐ (all start once every round-1 answer exists;
                                                 │  skipped if the panel already agrees)
                                                 ├─ judge x N ──────┐  (concurrent with each other
                                                 └─ synthesis ──────┴─ final eval ─ shadow judge
                                                                       and with synthesis)
```

- **Panel** (`fanout.py`): every call starts at once. `max_concurrency` caps in-flight calls *per
  provider*, so a provider that is slow or rate limited cannot hold up the others; the provider's own
  limiter (`provider_limits`) enforces its quota inside the provider. The per-model timeout starts
  once the call has a fan-out slot. Optional `early_return` stops waiting `grace_ms` after
  `quorum` answers (never fewer than `min_successful_responses`) and cancels the stragglers; optional
  `hedge_after_ms` asks another panel-role model when a member is still silent, and whichever answers
  first stands in for that member. A cancelled call is recorded in the ledger with its cost
  unknown, because the provider may still bill it.
- **Refinement**: each member's revision needs every peer's round-1 answer, so a round starts when
  the slowest member is in, and then all revisions run together. A round is skipped when the
  panel's agreement already reaches `refinement.skip_above_agreement`.
- **Judge and synthesis**: per-answer judge calls run together, and the whole judge stage runs
  alongside synthesis (`ConcurrentStages`) unless the strategy sets `judge_feeds_synthesis`, in
  which case synthesis waits and receives the judge's scores. If one stage fails, the other is
  cancelled and the original exception is raised.
- **Shadow baseline**: it needs only the task prompt, so `ShadowStartStage` starts it right after
  the context check and `ShadowStage` collects it after Fusion's answer is final. Fusion's wall time
  is stamped before the wait and shadow calls are excluded from Fusion's cost and critical path.
  A run that halts or fails cancels the baseline call.
- **Timeline**: every `CallRecord` has `started_at_ms` on the run's clock, and
  `RunLedger.timeline()` returns each call as a start and end span, so overlap can be read (and
  tested) from the ledger.

Config keys and failure semantics are in [CONFIGURATION.md](CONFIGURATION.md#fan-out). A panel call
that is still pending when the global timeout hits is attributed to its own model. The MCP request
is still a normal blocking request from Claude Code's perspective.

### Cascade, aggregators and caps

Three mechanisms cut what a run spends without a model call of their own. All of them read the
claim clusters and the ledger, never an answer's wording.

- **Cascade** (`cascade.py`, run by `PanelStage`). The members are sorted cheapest first by catalog
  list price and the first `cascade.first` form wave one. `measure_claims` (the measurement
  `ClaimsStage` also uses) and `decide` judge that wave: it may stop only if two or more models
  answered, `agreement_score` reaches the threshold, no cluster is contradicted and the router's
  risk is not `high`. Stopping sets `RunState.cascade.exited_early`; the panel is trimmed to wave
  one (so coverage is measured over the models actually asked), `RefineStage` does nothing and
  `AggregateStage` uses `early_aggregator`. Otherwise wave two runs the rest, `merge_fanouts`
  combines the waves (wall times add, the quorum is judged again over every model asked) and the
  run continues as a panel. A wave-one call that fails counts as "fewer than two answered", so a
  flaky cheap model escalates rather than answering alone. `CascadeOutcome` (reason, agreement,
  threshold, risk, who ran) is stored with the run.
- **Aggregators** (`aggregate.py`). `aggregator_for(strategy, cascade_exited_early)` picks one;
  `digest`, `vote` and `best_of` are pure functions of the clusters and answers. `vote` keeps the
  `consensus` clusters, `best_of` scores each model by the mean share of the other models backing
  its clusters (halved when disputed) and returns that model's answer as written, and `digest`
  lists consensus, disputed and single-model clusters ahead of every answer for Claude Code to
  merge. `llm` stays in `synthesize.py`.
- **Caps** (`budget_guard.py`, `routing/budget.py`). `BudgetStage` runs `preflight`: `plan_calls`
  lists the calls a strategy makes with assumed token counts (prompt size from the redacted text;
  fixed overheads and output sizes as constants in `routing/budget.py`), `forecast_calls` prices
  them from the catalog, and `down_shifts` yields cheaper strategies until one fits or none is
  left (a halt with reason `budget`). The result is stored as `RunState.preflight`. During the run
  a `BudgetGuard` on `RunState` is asked before each optional stage (`refinement`, `judge`,
  `synthesis`, a cascade's `escalation`): it adds the money held by stages already approved and
  not yet in the ledger (`release` frees it), compares with `max_cost_usd`, and compares the
  slowest call so far with the time left to `max_latency_s`. A refused stage degrades instead of
  failing: no refinement, deterministic judging, the digest instead of a synthesis, the first wave
  instead of escalation. `BudgetReport` (caps, forecasts, shifts, skipped stages) is stored with
  the run. Settings: [CONFIGURATION.md](CONFIGURATION.md#cost-and-latency-caps).

An optional **response cache** (`cache.py`, `BasePipeline.run`) returns an earlier answer for an
identical request in real mode; see [CONFIGURATION.md](CONFIGURATION.md#response-cache). Its key is
a hash of the redacted task, the task type, the whole strategy definition and `max_models`, so
changing any of them is a miss.

### Refinement (mixture-of-agents)

`src/fusion/orchestration/refine.py` runs after fanout, once for each round the strategy asks for
beyond the first (`rounds - 1`; the packaged `panel-refine` has `rounds: 2`). Each successful panel
model receives the peer answers anonymized as "Response A/B/C" plus its own answer and returns a
revised answer. Failures keep the previous answer. Refined answers feed the judge, disagreement analysis, and synthesis;
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
the `fusion_stats` MCP tool, and a one-line lifetime footer on every real-mode run's
`display_markdown` at `detail: full` (benchmark mode omits it).

### Claims and agreement

Panelists do not write free text. Every panel and refinement call carries the JSON Schema of
`PanelAnswer` (`orchestration/claims.py`) as its `response_schema`, so providers that enforce
structured output return a `summary`, a `confidence` and a list of atomic `Claim`s; providers that
cannot are given the schema in the prompt. A claim has a `kind` (finding, hypothesis,
recommendation, risk, test), an optional `severity` (low, med, high, critical), `file`, `line` and
`evidence`. A reply that is not valid claims JSON is read from its list items, flagged in the run's
warnings, and counts against coverage.

`ClaimsStage` then works without any model call:

1. `cluster_claims` groups claims across models. Two claims are the same point when they have the
   same `kind` and either cite the same file within 3 lines, or their normalized wording (lowercased,
   stop words dropped, light stemming) has Jaccard similarity of at least 0.5 (0.34 when they cite
   the same file). A cluster holds at most one claim per model, and models are visited in name
   order, so the result does not depend on arrival order.
2. `agreement_score` sorts the clusters into **consensus** (backed by at least two models and half
   the panel), **unique** (one model), **contradicted** (backed by several, with severities two or
   more levels apart) and partial. `score` is the mean over clusters of `(support - 1) / (n - 1)`,
   halved for contradicted ones, and 0 for fewer than two models. A model that shares nothing while
   the others share something is reported as an outlier (three or more models).
3. `calibrated_confidence` is `0.5 * agreement + 0.3 * evidence + 0.2 * coverage`, minus
   `0.15 * (contradicted clusters / clusters)`, clamped to [0, 0.95]. *Evidence* is the share of
   claims with a quoted `evidence` or a file and line; *coverage* is models that answered divided by
   models asked, times the share of answers that were valid claims JSON. With fewer than two answers
   there is nothing to agree with: the report is marked `low_information`, confidence is capped at
   0.50, and the run carries a warning saying so. A model's own `confidence` is reported but never
   used.

The synthesizer prompt receives the clusters (with status and who backs each) and the answers
rendered as Markdown, not raw JSON. For a `solo` run or a `digest` aggregator, and whenever the
synthesis is not JSON, the task fields (`critical_findings`, `ranked_hypotheses`, ...) are filled
from the clusters by kind, most severe and most agreed first (`output_parser.py`). The calibrated
confidence always replaces whatever confidence the synthesizer wrote.

### Evals

`src/fusion/evals/engine.py` coordinates the checks and scores:

- safety checks always run on every answer: secret leakage, dangerous shell commands, cited files
  that are not among the provided ones, and a missing `test` claim in a coding answer;
- the LLM judge runs only when the strategy's `judge` is `light` or `full` and a judge model is
  available (the default, `off`, makes no judge call);
- without a judge (or when its call fails) per-answer scores are measured from the claims
  (`evals/structural.py`): the share of claims that cite a file, carry evidence, propose an action
  or are rated for severity. Dimensions that need a judge or a reference stay at a neutral 0.5, and
  an answer that was not valid claims JSON scores neutral throughout;
- the final evaluation (`evals/final_eval.py`) takes `confidence` from the agreement report and
  derives usefulness (share of recommendation and test clusters), readiness (share with file and
  line), test plan (a test cluster exists, for coding tasks) and residual risk (the highest
  severity found) from the clusters. With no claims, as in a halted run, they are all 0.

No score depends on the wording of an answer. The aggregate score in `evals` is a weighted blend of
these measures (context, agreement, answer quality, final quality, provider success rate) less
penalties for unsupported claims and residual risk.

### Synthesis

For an `llm` aggregator (the other aggregators are described under
[Cascade, aggregators and caps](#cascade-aggregators-and-caps)) the synthesizer prompt receives:

- the original task;
- the claim clusters and a short agreement summary;
- each panel answer rendered as Markdown;
- the requested JSON schema for the task's fields.

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
