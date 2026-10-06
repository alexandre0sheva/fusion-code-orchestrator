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
`budgets`, `fanout`, `refinement`, `cache`, `strategies`, `budget_strategies` and `baselines`. A misspelled section is an error with a
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

## Command line

`fusion --help` lists every command and `fusion COMMAND --help` its flags and an example. From
install to a first answer:

```bash
export ANTHROPIC_API_KEY=...          # and/or OPENAI_API_KEY, GOOGLE_API_KEY
fusion doctor                         # checks Python, keys, config, database, the MCP server
fusion ask "How should I retry a failed HTTP call?"
```

| Command | Does |
|---------|------|
| `fusion ask PROMPT [-f FILE]...` | Ask the panel any coding question; `-f` attaches files |
| `fusion review-diff -f DIFF` | Review a diff (`git diff main \| fusion review-diff -f -`) |
| `fusion debug [ERROR] [-f TRACE] [--logs-file LOG]` | Ranked root causes and how to check each |
| `fusion decide [QUESTION]`, `fusion plan [FEATURE]` | An architecture decision, an implementation plan (`-f FILE` reads the text from a file) |
| `fusion eval-answer`, `fusion compare-claude-runs` | Score an answer, compare Claude Code runs |
| `fusion stats`, `fusion runs list\|show\|costs\|compare-baseline\|export` | Spend, savings and the stored run history |
| `fusion config paths\|show\|validate`, `fusion strategies list`, `fusion models list\|check` | Configuration, strategies, the model catalog |
| `fusion doctor` | Is this machine ready? See [below](#fusion-doctor) |
| `fusion dashboard [--port 8765]` | A read-only local web view of spend, runs, benchmarks and configuration; see [Dashboard](#dashboard) |
| `fusion bench ...`, `fusion install ...`, `fusion mcp`, `fusion init`, `fusion version` | [Benchmarks](BENCHMARKING.md), [client setup](INTEGRATIONS.md), the MCP server, a starter config |

**Flags every run command shares.** The main input is an argument, `-f/--file` or stdin (`-`).
`--context TEXT` and `--context-file F` add background the panel cannot see; `--strategy NAME` and
`--max-cost USD` choose and cap the run (see [Strategies](#strategies-and-budgets)); `--detail
compact|full` (default `compact`); `--json`; `--mock` runs offline on the deterministic mock
provider; `--db-path`; `-q/--quiet`. The v0.1 spellings (`--error-file`, `--feature-file`,
`--error`, `--question`) still work.

**Output.** The answer goes to stdout, as Markdown or, with `--json`, as the same record the MCP tool
returns (compact by default, `--detail full` adds claims, usage, cost comparison and routing).
Progress, warnings and errors go to stderr, so `fusion ask ... --json | jq` always parses. Every
data command has `--json` (`runs show` and `runs compare-baseline` always print JSON; `runs export`
prints one object per line).

**Exit codes.**

| Code | Meaning |
|------|---------|
| 0 | Success (a `partial` run that returned the panel's digest counts) |
| 1 | The command failed: bad config, no provider key, unreadable file, unknown run, a failed `doctor` check |
| 2 | The command line is wrong: missing input, unknown option, `--max-cost 0` |
| 3 | The run finished with no answer (`halted`: not enough context, no quorum, over budget, timed out) |

Failures print `Error: ...` and a hint on one or two lines, never a traceback; `fusion --verbose
COMMAND` raises the original error with its traceback, which is what a bug report needs. With no
provider key set and no local provider enabled, a run command stops at once and says how to fix it.

**Live view.** While a run works, stderr shows what it is doing. On a terminal it is a panel with
the current stage and one row per model call (spinner, then a tick or a cross with tokens, cost and
latency), plus the running cost beside what the same tokens would cost on the baseline model.
Shadow-baseline calls are shown dimmed and are not added to the cost. Anywhere else (a pipe, a file,
CI) it is one plain line per event:

```text
panel: asking 2 models
  ok    panel      claude-haiku       1,204 in / 310 out  $0.0012  3.4s
  FAIL  panel      gpt-luna           TimeoutError: Timed out after 30.0s
synthesizing
```

`-q` turns it off.

### `fusion doctor`

Checks, in the order to fix them: the Fusion and Python versions and whether `uv` is on `PATH`; the
configuration files; each provider's key (shown as `…` and its last four characters, never whole);
catalog staleness and retiring models; that the database folder is writable; that `fusion mcp`
starts, speaks only JSON-RPC on stdout and lists its tools (it runs on the mock provider and a
scratch database, so it needs no keys and touches nothing of yours; `--no-mcp` skips it); and each
client's setup (Claude Code, Cursor, Codex; a client you do not use shows as `info`). A failing
check prints a `fix:` line. `--live` also asks each provider for its model list, which is free and
proves the key and the network work; it never sends a completion, so it costs nothing. Exit code 1
when a check is an error (`--strict`: or a warning); `--json` prints `{ok, checks: [{name, status,
detail, fix}]}`.

### Dashboard

```bash
fusion dashboard                  # http://127.0.0.1:8765/   (--port N, --open to launch a browser)
```

A read-only web view of what Fusion has recorded, with no build step and nothing loaded from
another site. It reads the same database as the CLI and the MCP server (`--db-path`,
`FUSION_DB_PATH`) and the benchmark folder (`FUSION_BENCH_DIR`).

| View | Shows |
|------|-------|
| Overview | Lifetime spend against the baseline model's estimate, what you kept, the shadow A/B win rate with its 95% interval, runs per day, and where runs go by task and strategy |
| Runs | Every run, filterable by task, strategy, status and text; open one for its answer, its claims grouped by how many models agree, a latency timeline of every model call, cost by stage, tokens per second and the redacted input |
| Benchmarks | Benchmark runs, each run's full report, and a comparison of two runs |
| Config | The strategies and models in effect, prices and staleness warnings, where each setting comes from, and which provider keys are set (never their values) |

It listens on 127.0.0.1 only, answers only loopback host names, and never changes your data. Run
detail shows the **redacted** copy of what was asked, with the number of secrets replaced; the
original is shown only when `FUSION_LOG_RAW_PROMPTS=true`, and a banner says so. Charts have a table
view and tooltips that work from the keyboard; the page follows the system light or dark setting
(a button overrides it). An empty database shows the command that fills it. The baseline figures are
estimates (the same tokens at the baseline model's list price), as in [COSTS.md](COSTS.md).

### Deprecated commands

`fusion review`, `fusion list-runs`, `fusion run-mock` and `fusion compare-cost` print a notice on
stderr and still work in 0.2.x; they are removed in 0.3.0. Use `fusion review-diff -f DIFF`,
`fusion runs list`, any run command with `--mock`, and `fusion runs compare-baseline RUN_ID`.

### Shell completion

```bash
fusion --install-completion     # bash, zsh, fish or PowerShell, detected from your shell
fusion --show-completion        # print the script instead, to put where you like
```

Restart the shell afterwards. Completion covers commands, subcommands and flags.

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
| `FUSION_BENCH_DIR` | Where benchmark runs, their database, the response cache and `spend.json` live | `bench-results` under the project directory |
| `FUSION_BROWSER_PATH` | Chromium or Chrome binary the optional `[bench-visual]` evaluators use instead of Playwright's own download | Playwright's Chromium |
| `FUSION_JUDGE_ACCURACY_FLOOR` | Accuracy (0 to 1) the agentic judge must reach in its latest `calibrate-judge --artifacts` run for a study to get a headline verdict ([BENCHMARKING.md](BENCHMARKING.md#frontend-and-performance-tasks-evidence-and-the-agentic-judge)) | `0.8` |
| `FUSION_SANDBOX_ISOLATION` | Benchmark sandbox: `auto` uses network and write isolation when the platform has it, `require` refuses to run without it, `off` disables it ([SECURITY.md](../SECURITY.md#benchmark-sandbox)) | `auto` |
| `FUSION__<SECTION>__<KEY>` | Override one config key (see [Layers and locations](#layers-and-locations)) | unset |
| `FUSION_SHADOW_MODE` | Shadow A/B against the real baseline: `off`, `sampled`, `always` | `off` |
| `FUSION_SHADOW_SAMPLE_RATE` | Fraction of runs shadowed in `sampled` mode | `0.2` |
| `FUSION_LOG_RAW_PROMPTS` | Log unsanitized prompts (dangerous) | `false` |
| `FUSION_DEFAULT_PROVIDER` | Set to `mock` for offline mode | unset (live) |
| `FUSION_TOOL_SOFT_TIMEOUT_S` | Seconds an MCP tool call may run before it returns the panel's digest with a warning; `0` or `off` disables ([INTEGRATIONS.md](INTEGRATIONS.md#progress-cancellation-and-the-soft-time-limit)) | `90` |

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
code change. A strategy lists each model alias once; to ask one model several times (a
*self-mixture*) use the catalog's sample aliases `claude-haiku-s2`, `claude-haiku-s3`, `gpt-luna-s2`
and `gpt-luna-s3`, the same models at the same prices that no default panel picks, with a different
`temperature` (or `reasoning_effort`, for models that ignore sampling parameters) per member.

```yaml
strategies:
  my-panel:
    kind: panel                  # solo | panel | cascade
    members:                     # catalog aliases; solo has exactly one
      - model: claude-haiku
      - model: gpt-luna
        role: security_reviewer  # "auto" (default) = the catalog persona, else the task prompt
        temperature: 0.2         # optional per member
        reasoning_effort: low    # optional per member
    rounds: 2                    # 1 = answer once; 2 = plus one peer-refinement round
    aggregator: llm              # llm | digest | vote | best_of | verified (benchmark only)
    aggregator_model: claude-sonnet   # llm only; omit for the catalog's synthesizer role
    judge: off                   # off | light | full
    judge_feeds_synthesis: false # true = the synthesizer reads the judge's scores (and waits)
    max_cost_usd: 0.05           # optional hard cap, see "Cost and latency caps"
    max_latency_s: 60            # optional; stages that would not fit are skipped
    fanout:                      # optional: this strategy's own latency controls (see Fan-out)
      early_return: {quorum: 2, grace_ms: 1500}
  my-cascade:
    kind: cascade                # cheapest members first, the rest only if they disagree
    members: [{model: claude-haiku}, {model: gpt-luna}, {model: gemini-flash}]
    cascade:
      first: 2                   # how many of the cheapest members answer first (at least 2)
      agreement_threshold: 0.7   # stop there when their agreement is at least this
      early_aggregator: vote     # vote | best_of: how that early answer is made (no model call)
      escalate_on_high_risk: true  # a high-risk task always asks the whole panel
    aggregator: llm              # what merges the answers once it has escalated
    aggregator_model: claude-sonnet
budget_strategies:
  medium: my-panel
```

| Strategy | Runs | Calls per task |
|----------|------|----------------|
| `solo-frontier` | Claude Opus 5.5 | 1 |
| `solo-sol` | GPT-6.1 Sol | 1 |
| `solo-cheap` | Claude Haiku 4.5 | 1 |
| `solo-luna` | GPT-6 Luna | 1 |
| `panel-duo` (default) | Haiku 4.5 + GPT-6 Luna, merged by Haiku 4.5; chosen by the [v0.2.0 study](BENCHMARK_RESULTS.md), with no spare model if one fails | 2 + 1 |
| `panel-cheap` | Haiku 4.5 + GPT-6 Luna + Gemini 3.8 Flash, merged by Haiku 4.5 | 3 + 1 |
| `panel-cheap-strong-synth` | the same panel, merged by Claude Sonnet 5.5 | 3 + 1 |
| `panel-refine` | the same panel plus one refinement round, merged by Haiku 4.5 | 3 + 3 + 1 |
| `panel-digest` | the same panel, no synthesis: the answers come back for Claude Code to merge | 3 |
| `panel-vote` | the same panel, no synthesis: only the points a majority of models backed come back | 3 |
| `panel-cascade` | the two cheapest of the panel first; if they agree, their shared points come back (2 calls), otherwise the rest answer and Claude Sonnet 5.5 merges | 2, or 3 + 1 |
| `best-of-n-verified` | **benchmark only** (refused elsewhere, it runs code): the same panel each write a patch and the task's visible tests pick one, no synthesis; see [BENCHMARKING.md](BENCHMARKING.md#coding-tasks-and-the-sandbox) | 3 |
| `panel-local` | Ollama and LM Studio models; falls back to the cloud panel with a warning when none are enabled | 2 + 1 |

How the fields behave:

- **`solo`** returns the model's answer as is (one call, no synthesis). Benchmark baselines are solo
  strategies, so a baseline and Fusion share one code path and one cost ledger.
- **`rounds`** counts answer rounds: `rounds: 3` is the panel plus two refinement rounds. In a
  refinement round each member sees the other members' anonymized answers and revises its own; a
  member whose call fails keeps its previous answer. Timeouts and the minimum panel size are under
  [Refinement](#refinement-mixture-of-agents).
- **`aggregator`** says how the answers become one. `llm` makes one synthesizer call. The others make
  none, so aggregation is free: `digest` returns every answer plus the shared, disputed and
  single-model points, and **Claude Code is the aggregator** (the tool descriptions tell it to keep
  what several models agree on and check the rest against the code); `vote` returns only the points
  a majority of the models backed (and says so when there are none); `best_of` returns the one
  answer that the other models' claims back most, as written (ties go to the answer with more
  evidence, then to the earlier member). `verified` (benchmark mode only, for coding tasks) runs the
  task's visible tests on each panelist's patch and returns the best. `vote`, `best_of`, `digest`
  and `verified` take no `aggregator_model`. When the task's answer is a code patch, `vote` returns
  the patch most models gave (see [BENCHMARKING.md](BENCHMARKING.md#coding-tasks-and-the-sandbox)).
- **`judge`**: `off` runs only the deterministic checks and makes no judge call (the default, so a
  run costs only its panel and aggregator). `light` has the judge model score each answer.
  `full` does the same and also records a check of the judge's own output under
  `evals.judge_quality`. Which model judges is the task's `judge_model` in `routing_policies.yaml`.
- **`judge_feeds_synthesis`** (default `false`): by default the judge and synthesis run at the same
  time, since the synthesizer does not read the judge's scores. Set it to `true` for the synthesizer
  to receive them; synthesis then waits for the judge. It needs `judge: light` or `full` and an
  `llm` aggregator.
- **`kind: cascade`** runs the `cascade.first` cheapest members (by catalog list price; ties keep the
  configured order; a model with no price counts as dearest) as a first wave. If at least two
  answered, their agreement is at least `agreement_threshold`, no point has a disputed severity and
  the task is not high risk (unless `escalate_on_high_risk: false`), the run ends there:
  `early_aggregator` (`vote` or `best_of`) makes the answer, no refinement or synthesis happens and
  no other model is called. Otherwise the remaining members answer, and `rounds` and `aggregator`
  apply as for a panel. A cascade needs more members than `first`; if `max_models` leaves fewer it
  runs as one panel and says so. The default `agreement_threshold` of 0.7 is provisional until the
  benchmark study tunes it. `routing.reasons` and the stored run's `cascade` record which way it
  went and why.
- A model may appear only once in a strategy.
- `max_cost_usd` and `max_latency_s` are caps; see [Cost and latency caps](#cost-and-latency-caps).

### Cost and latency caps

`max_cost_usd` on a strategy is enforced before and during a run. An MCP tool call may pass its
own `max_cost_usd`; the lower of the two applies, so a call can tighten a strategy's cap but never
loosen it, and a strategy with no cap gets one for that call only.

1. **Before any call**, the planned calls are priced from the catalog (assumed token counts, the
   same arithmetic as the table in [COSTS.md](COSTS.md#what-a-task-costs-by-strategy), using the
   prompt's real size). If the forecast is over the cap, the strategy is shifted down one step at a
   time until it fits, and the response says so in `warnings` and `routing.reasons`: drop the
   refinement rounds and judge calls, then drop the dearest member until two are left, then run the
   cheapest member alone. If even that is over the cap, the run is refused before any model is
   called (cost $0) with a message naming the strategy and the cap to raise. A cascade is forecast
   for its first wave only. When a model has no catalog price the forecast cannot be checked, which
   is warned about, and the cap is still enforced during the run.
2. **During the run**, before refinement, the LLM judge, synthesis and a cascade's escalation, the
   stage is priced (from the answers' real sizes) against the money already spent. A stage that
   would pass the cap is skipped with a warning: refinement stops, the judge falls back to
   deterministic checks, synthesis is replaced by the free panel digest, and a cascade returns its
   first wave's answer instead of escalating. Stages that run together reserve their share.

`max_latency_s` is enforced during the run only (latency cannot be forecast before a call has been
made): a stage is skipped when the elapsed time plus the slowest call so far would pass the cap.
The panel itself is never cut short by it; use `fanout.global_timeout_seconds` for that. A run that
still ends over a cap adds a warning. Costs are estimates until the ledger has them, so treat a cap
as a tight budget, not an accounting guarantee.

Budgets are aliases kept for compatibility:

| Budget | Strategy |
|--------|----------|
| `low` | `solo-cheap` |
| `medium` | `panel-duo` |
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
| Secret redaction | on | off, so ground-truth tasks reach the models as written; a study can turn it on |
| Judge | the strategy's `judge` (default `off`) | the strategy's `judge` |
| Sampling | provider defaults | temperature `0` and seed `0` (a study sets its own seed per repeat) unless a member sets its own temperature (seeds reach OpenAI, Google and Ollama; Anthropic has none) |
| Streaming | off | on for every model that supports it, so time to first token and decode speed are measured |
| Prompts over a model's context window | trimmed, with a warning | sent as is |
| Shadow A/B | per `FUSION_SHADOW_MODE` and `shadow_baseline` | never |
| Lifetime-stats footer | appended at `detail: full` | omitted |
| Stored ledger | full | full |

`Pipeline.run` also takes `seed` and `redact` to override these two for one run, and `ledger` to
keep the run's calls when a stage raises. The runner that drives benchmark mode, its provider-response
cache and its spend limits are described in [BENCHMARKING.md](BENCHMARKING.md#benchmark-mode-fusion-bench).

## Response cache

Off by default. With `cache.enabled: true`, a real-mode run whose redacted task text, task type,
strategy (its whole definition) and `max_models` match an earlier completed run less than
`ttl_seconds` old returns that answer without calling any model: no cost, a warning saying it came
from the cache, and no new run in the stored history or lifetime stats. It is kept in the memory of
one process (an MCP server session), least recently used entries go first, a halted run is never
cached, and benchmark mode neither reads nor writes it.

```yaml
cache:
  enabled: true
  ttl_seconds: 900     # how long an answer may be reused
  max_entries: 128
```

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

A strategy may set either control for itself with `fanout:` (`early_return` and `hedge_after_ms`
only; the other limits stay the routing policy's), which is how the latency benchmark compares them
on one panel.

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
