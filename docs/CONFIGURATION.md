# Configuration

Canonical reference for environment variables, YAML config files, strategies, budgets, fan-out and
refinement. Cost and pricing methodology lives in [COSTS.md](COSTS.md).

## Layers and locations

Settings come from five layers; a later layer overrides an earlier one:

1. **Packaged defaults**: `catalog.yaml`, `routing_policies.yaml`, `strategies.yaml`, `lenses.yaml`,
   `approaches.yaml` and `baseline.yaml` inside the package (`src/fusion/config/`). Do not edit these in an installed copy.
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
| `src/fusion/config/catalog.yaml` | Model catalog: aliases, provider model IDs, capabilities, enable flags, verified prices (with long-context rows) and the `tiering:` rule that turns prices into tiers. |
| `src/fusion/config/routing_policies.yaml` | Per-task judge model and context threshold; fan-out and refinement settings. |
| `src/fusion/config/strategies.yaml` | Strategies (which models answer, rounds, aggregation, judging), the budget-to-strategy table and `strategy_aliases` (old strategy names). |
| `src/fusion/config/lenses.yaml` | Panel lenses: the area each member of a review, debugging, plan or architecture panel covers (see [Lenses](#lenses)). |
| `src/fusion/config/approaches.yaml` | Panel approaches: the stance each member of a plan, decision, ask or patch panel answers from (see [Approaches and `select`](#approaches-and-select)). |
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
| `fusion ask PROMPT [-f FILE]...` | Ask the panel any coding question; `-f` names project files for it to read, with the rules of the MCP `paths` input: under the current directory, skipping secret, ignored, binary and oversize files ([Reading the project](INTEGRATIONS.md#reading-the-project)) |
| `fusion review-diff --diff-ref REF` or `-f DIFF` | Review `git diff REF` read by Fusion itself (`--diff-ref main...HEAD`), or a diff file (`git diff \| fusion review-diff -f -`) |
| `fusion digest QUESTION [-f FILE]... [--glob PATTERN]... [--diff-ref REF]` | Have cheap models read large material and answer in at most `--max-answer-tokens` (default 600) |
| `fusion patch TASK -f FILE... [--tests-cmd CMD] [--verify sandbox\|host\|none] [--diff-only]` | A patch cross-checked by the panel's own checks (and `CMD`) in a sandbox copy of the project, printed as a unified diff and never applied; `--diff-only` prints the diff alone (exit 3 when there is none), e.g. `> fix.patch` ([Patches](INTEGRATIONS.md#patches)) |
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
configuration files; each provider's key (shown as `…` and its last four characters, never whole;
with `OPENROUTER_API_KEY` set, a vendor without its own key is `ok` because its models run through
OpenRouter), and, with that key, the [OpenRouter privacy setting](#openrouter-routes-and-data-policy)
it cannot verify; catalog staleness and retiring models; whether `fusion_propose_patch` can use a
sandbox here (`sandbox-exec` on macOS, `unshare` on Linux, which takes away the network but does
not confine file writes; a warning where neither works, since the tool then falls back to host
mode; `info` when [`verify.sandbox_enabled`](#patch-verification) is off); that the database folder is writable; that `fusion mcp`
starts, speaks only JSON-RPC on stdout and lists its tools (it runs on the mock provider and a
scratch database, so it needs no keys and touches nothing of yours; `--no-mcp` skips it); and each
client's setup (Claude Code, Cursor, Codex; a client you do not use shows as `info`). A failing
check prints a `fix:` line. `--live` also asks each provider for its model list, which is free and
proves the key and the network work, and runs the OpenRouter price and privacy checks of
`fusion models check --live` ([COSTS.md](COSTS.md#keeping-prices-honest)); it never sends a
completion, so it costs nothing. Exit code 1
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
| `OPENROUTER_API_KEY` | OpenRouter API key: the open-weight models, and the route for any vendor whose key is unset ([OpenRouter](#openrouter-routes-and-data-policy)) | — |
| `FUSION_OPENROUTER_PRIVACY` | Set to `confirmed` once your OpenRouter account excludes providers that may train on prompts; turns the `fusion doctor` reminder into `ok` (Fusion still cannot read the setting) | unset |
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
| `FUSION_LOG_RAW_PROMPTS` | Keep the original input, secrets included, in the run database; by default only the redacted copy is stored (dangerous) | `false` |
| `FUSION_REDACT_ENTROPY` | Redact long random-looking tokens that match no known key shape: `on` or `off` ([SECURITY.md](../SECURITY.md#secret-redaction)) | `on` |
| `FUSION_REDACT_ENTROPY_THRESHOLD` | Bits of Shannon entropy per character a token needs for the entropy rule to redact it | `4.5` |
| `FUSION_REDACT_ENTROPY_MIN_LENGTH` | Shortest token, in characters, the entropy rule considers | `32` |
| `FUSION_DEFAULT_PROVIDER` | Set to `mock` for offline mode | unset (live) |
| `FUSION_READ_ROOT` | Project root for `paths`, `globs` and `diff_ref` when the MCP client names no root ([INTEGRATIONS.md](INTEGRATIONS.md#reading-the-project)) | the server's working directory |
| `FUSION_WORKSPACE_MAX_FILE_BYTES` | Largest project file Fusion reads; bigger ones are skipped | `128000` |
| `FUSION_WORKSPACE_MAX_BYTES` | Most bytes one call reads from the project (files and diff together) | `320000` |
| `FUSION_TOOL_SOFT_TIMEOUT_S` | Seconds an MCP tool call may run before it returns the panel's digest with a warning; `0` or `off` disables ([INTEGRATIONS.md](INTEGRATIONS.md#progress-cancellation-and-the-soft-time-limit)) | `90` |

How the shadow A/B works and how to read its results: [BENCHMARKING.md](BENCHMARKING.md#shadow-ab-live-measurement).

## Providers

| Provider | Variables | Default |
|----------|-----------|---------|
| Anthropic | `ANTHROPIC_API_KEY` | Enabled when the key is set |
| OpenAI | `OPENAI_API_KEY` | Enabled when the key is set |
| Google | `GOOGLE_API_KEY` | Enabled when the key is set |
| OpenRouter | `OPENROUTER_API_KEY` | Enabled when the key is set; serves the open-weight models and stands in for a vendor without a key |
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

### OpenRouter: routes and data policy

OpenRouter is a fourth provider (OpenAI-compatible Chat Completions at
`https://openrouter.ai/api/v1`). It serves the open-weight models in the catalog (DeepSeek, MiniMax,
Qwen, GLM, MiMo, Hunyuan, Muse, Mistral, Kimi, Grok; `fusion models list`), and it is a second
route to the Anthropic, OpenAI and Google models. Each of those has two catalog fields:

| Field | Values | Meaning |
|-------|--------|---------|
| `route` | `auto` (default), `direct`, `openrouter` | `auto`: the vendor's own API when its key is set, OpenRouter otherwise. `direct`: only the vendor's API. `openrouter`: always OpenRouter |
| `openrouter_id` | for example `anthropic/claude-haiku-5.5` | The model's ID on OpenRouter; without it the model has no OpenRouter route |

So `OPENROUTER_API_KEY` alone is enough to run every default strategy. Each call records the route
that served it: the run's ledger shows provider `openrouter` and the OpenRouter ID. A call through
OpenRouter is billed what OpenRouter reports (`usage.cost`), which the ledger records instead of the
catalog estimate; the estimate before the run (and `max_cost_usd`) still uses the catalog price,
which for a vendor model is the vendor's list price (`fusion models check --live` flags where
OpenRouter's differs by more than 5%). Through OpenRouter the Anthropic-only features are not used:
no prompt caching breakpoints and no Anthropic structured-output mode (JSON schema goes through
OpenRouter's `response_format` instead).

**Data policy (no training on prompts).** OpenRouter forwards each request to one of several hosts
for the model. Fusion's rule is that no host which may *train* on prompts gets them; hosts that
retain or log prompts without training are allowed. OpenRouter has no per-request switch for that
(its `data_collection: "deny"` also drops every host that stores data, and zero-data-retention is
stricter still), so the rule is enforced in two layers:

1. **Your account setting (required).** At <https://openrouter.ai/settings/privacy>, turn off
   providers that may train on your inputs (for paid models). OpenRouter then never routes to them.
   Fusion cannot read this setting: `fusion doctor` shows `openrouter-privacy` as a warning until
   you set `FUSION_OPENROUTER_PRIVACY=confirmed`, and even then says it did not verify it.
2. **An ignore list on every request.** Each request sends `provider: {ignore: [...],
   data_collection: "allow", require_parameters: true}`. The list is `openrouter.provider_ignore`
   in the catalog: the providers OpenRouter's provider list marks as possibly training on prompts
   (on 2026-11-17: DeepSeek, Liquid, NVIDIA, Thinking Machines; DeepSeek's own models still run, on
   other hosts). A config layer can add slugs but never remove these. `fusion models check --live`
   re-reads OpenRouter's list and reports any training provider missing from it as `POLICY`.

Requests carry a generic `X-Title: fusion-code-orchestrator` header and nothing that identifies
you. What OpenRouter itself sees is in [SECURITY.md](../SECURITY.md#what-leaves-the-machine).

### Rate limits and reasoning effort

Three catalog settings tune provider calls (all in `src/fusion/config/catalog.yaml`):

| Setting | Where | Effect |
|---------|-------|--------|
| `provider_limits.<provider>.max_concurrent` | top level | Most in-flight requests to that provider, shared by every call (panel, refinement, judge). |
| `provider_limits.<provider>.rpm` | top level | Most request starts in any 60 s window. Omit either key for "unlimited". Set both to your account's real quota. |
| `default_reasoning_effort` | per model | Effort sent when a request does not choose one (`none`..`max`; Gemini maps to `low`/`medium`/`high`; OpenRouter gets it as `reasoning: {effort}`). Panel models default to `low`; the baseline and synthesizer keep provider defaults so comparisons stay fair. |
| `persona` | per model | Panel persona (for example `security_reviewer`), appended to the end of the prompt so every member shares one system prompt and prompt prefix. |
| `observed_ttft_ms`, `observed_decode_tps`, `observed_on` | per model | Measured median time to first token and decode rate from a live study (seeded from the 0.2.0 study; the 0.3.0 study refreshes them). They time the early-return grace and rank models for `speed: fast`, hedges and fallbacks; a model without them ranks after every model with them. |
| `max_tokens` | per model | Output cap Fusion requests. Thinking tokens count against it, so reasoning models use 16384. |

## Strategies and budgets

A **strategy** says who answers a task and how the answers are combined. Every tool accepts an
optional `strategy` (a name from `strategies.yaml`); `fusion strategies list` prints them. Without
one, the legacy `budget` argument picks the strategy through `budget_strategies` (the default,
`medium`, is [`auto`](#difficulty-routing-strategy-auto)), and the response's `routing.strategy`
reports which one ran. Switching strategy is a config or argument change, never a
code change. A strategy lists each model alias once unless it uses [lenses](#lenses), which is
how a self-mixture is built now: the 0.2.0 temperature-sample aliases `claude-haiku-s2` and friends
are gone, since Claude's API no longer accepts sampling parameters.

Strategies renamed in 0.3.0 keep their old names as aliases for one release (`strategy_aliases`
in `strategies.yaml`): `panel-cheap` runs `panel-nano`, `solo-cheap` runs `solo-haiku` and
`solo-frontier` runs `solo-opus`. A config override written under an old name (for example
`strategies.panel-cheap.rounds: 2`) is applied to the new strategy; the run reports the new name.

```yaml
strategies:
  my-panel:
    kind: panel                  # solo | panel | cascade | auto (see Difficulty routing)
    members:                     # catalog aliases; solo has exactly one
      - model: claude-haiku
      - model: gpt-luna
        role: security_reviewer  # "auto" (default) = the catalog persona, else the task prompt
        temperature: 0.2         # optional; sent only if the catalog sets supports_sampling_params
        reasoning_effort: low    # optional per member
        lens: security           # optional: this member's lens (see Lenses); a+b holds two
        approach: minimal_fix    # optional, select strategies: this member's approach
    lenses: auto                 # auto | off; unset = auto for llm, digest, union, union+check
    approaches: auto             # auto | off; unset = auto for select, select+patch
    rounds: 2                    # 1 = answer once; 2 = plus one peer-refinement round
    aggregator: llm              # auto (the default) | llm | digest | vote | best_of | union
                                 # | union+check | select | select+patch
                                 # | verified | verified-synth (both benchmark only)
    aggregator_model: claude-sonnet   # llm, union+check, select; omit for the catalog's synthesizer
    judge: off                   # off | light | full
    judge_feeds_synthesis: false # true = the synthesizer reads the judge's scores (and waits)
    max_cost_usd: 0.05           # optional hard cap, see "Cost and latency caps"
    max_latency_s: 60            # optional; stages that would not fit are skipped
    fanout:                      # optional: this strategy's own latency controls (see Fan-out)
      early_return: {quorum: 2, grace_ms: 1500}   # or `off`
      hedge_after_ms: off
    skip_aggregator:             # optional, llm aggregator only: no synthesis call when they agree
      agreement_threshold: 0.6   # the answers must agree at least this much
      use: vote                  # vote | best_of | union builds the answer instead
      escalate_on_high_risk: true  # a high-risk task is always synthesized
    speed: balanced              # balanced | fast (rank members by measured speed)
    self_check: false            # true: each member checks its answer, reports an honest confidence
  my-cascade:
    kind: cascade                # cheapest members first, the rest only if they disagree
    members: [{model: claude-haiku}, {model: gpt-luna}, {model: gemini-flash}]
    cascade:
      first: 2                   # how many of the cheapest members answer first (at least 2)
      agreement_threshold: 0.7   # stop there when their agreement is at least this
      early_aggregator: vote     # vote | best_of | union: how that early answer is made (no call)
      escalate_on_high_risk: true  # a high-risk task always asks the whole panel
    aggregator: llm              # what merges the answers once it has escalated
    aggregator_model: claude-sonnet
budget_strategies:
  medium: my-panel
```

| Strategy | Runs | Calls per task |
|----------|------|----------------|
| `auto` (default) | estimates the request's difficulty and climbs the [escalation ladder](#difficulty-routing-strategy-auto) from the matching rung: Haiku 5.5 with a self-check, `panel-nano-auto`, `panel-mid`; never the frontier rung | 1 to 9 |
| `auto-frontier` | `auto` that may also end at the frontier rung (`solo-opus`) | 1 to 9 |
| `solo-<model>` | one model alone: `solo-haiku`, `solo-sonnet`, `solo-opus`, `solo-fable`, `solo-luna`, `solo-terra`, `solo-sol`, `solo-astra`, `solo-flash`, `solo-hydrogen`, `solo-carbon`, `solo-argon` | 1 |
| `solo-haiku-check` | Haiku 5.5 alone, asked for a self-check and an honest confidence (rung 0 of `auto`) | 1 |
| `panel-duo` (the panel default before `auto`) | Claude Haiku 5.5 + GPT-6 Luna; when they agree (0.6) their vote is the answer, otherwise Haiku 5.5 merges them; a failing model is replaced by another of its tier | 2, or 2 + 1 |
| `panel-nano` | Haiku 5.5 + GPT-6 Luna + Gemini 3.8 Flash, merged by Haiku 5.5 | 3 + 1 |
| `panel-mid` | GPT-6.1 Terra + Gemini 4 Hydrogen + Claude Sonnet 5.5, merged by Sonnet 5.5 | 3 + 1 |
| `panel-open` | open-weight models through OpenRouter: DeepSeek V4.1 Flash + MiniMax M3.1 Flash + Qwen 3.8 Flash + GLM-5.3, merged by Haiku 5.5; needs `OPENROUTER_API_KEY`; a 0.3.0 ablation candidate, not a default | 4 + 1 |
| `panel-mixed` | Haiku 5.5 + GPT-6 Luna + DeepSeek V4.1 Flash (OpenRouter), merged by Haiku 5.5; a 0.3.0 ablation candidate, not a default | 3 + 1 |
| `panel-cheap-strong-synth` | the `panel-nano` models, merged by Claude Sonnet 5.5 | 3 + 1 |
| `panel-refine` | the `panel-nano` models plus one refinement round, merged by Haiku 5.5 | 3 + 3 + 1 |
| `panel-digest` | the `panel-nano` models, no synthesis: the answers come back for Claude Code to merge | 3 |
| `panel-vote` | the `panel-nano` models, no synthesis: only the points a majority of models backed come back | 3 |
| `panel-nano-union` | the `panel-nano` models with lenses, merged by `union`: every lens's findings that the input backs, no synthesis | 3 |
| `panel-nano-select` | the `panel-nano` models, each answering from its own approach; one cheap Haiku 5.5 call picks the best answer (`select+patch`); for plans, decisions, asks and patches | 3 + 1 (small) |
| `panel-luna-approaches` | GPT-6 Luna asked once per approach (up to three copies), then one cheap call picks the best (`select`); a 0.3.0 ablation candidate, not a default | up to 3 + 1 (small) |
| `panel-nano-auto` | the `panel-nano` models with `aggregator: auto`: lenses and `union` on review and debugging, approaches and `select+patch` on everything else | 3 (+ 1 small) |
| `panel-luna-lenses`, `panel-haiku-lenses` | one model asked once per lens (up to three copies), merged by `union`: a self-mixture whose diversity comes from lenses; a 0.3.0 ablation candidate, not a default | up to 3 |
| `panel-cascade` | the two cheapest of the `panel-nano` models first; if they agree, their shared points come back (2 calls), otherwise the rest answer and Claude Sonnet 5.5 merges | 2, or 3 + 1 |
| `best-of-n-verified` | **benchmark only** (refused elsewhere, it runs code): the `panel-nano` models each write a patch and the task's visible tests pick one, no synthesis; see [BENCHMARKING.md](BENCHMARKING.md#coding-tasks-and-the-sandbox) | 3 |
| `verified-synth` | **benchmark only** (refused elsewhere, it runs code): the `panel-nano` models each write a complete patch and the checks that judge any fix; every patch runs against every check in a sandbox copy and the one passing most of the others' checks wins, a tie goes to one Haiku 5.5 selector call; see [BENCHMARKING.md](BENCHMARKING.md#coding-tasks-and-the-sandbox) | 3 + 1 on a tie |
| `panel-local` | Ollama and LM Studio models; falls back to the cloud panel with a warning when none are enabled | 2 + 1 |

Model tiers (`nano` to `premium`) are computed from catalog prices; see
[COSTS.md](COSTS.md#price-tiers) and `fusion models list`. `solo-opus` is the default baseline;
`solo-fable`, `solo-astra` and `solo-carbon` are the benchmark-only frontier baselines.

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
  evidence, then to the earlier member). `union` adds every member's findings together for a
  lensed panel (see [Lenses](#lenses)); `union+check` also makes one cheap call about the findings
  the union could not check. `select` and `select+patch` pick the best of the members' whole answers
  with one small call (see [Approaches and `select`](#approaches-and-select)). **`auto`**, the
  default when a strategy names none, becomes `union` for review and debugging and `select+patch`
  for plans, decisions, asks and patches. `verified` (benchmark mode only, for coding tasks) runs the task's
  visible tests on each panelist's patch and returns the best. `verified-synth` (benchmark mode
  only) has each member write its patch plus the checks that judge it and runs every patch against
  every member's checks in sandbox copies; a tie goes to one `select` call (`aggregator_model`), and
  it takes `rounds: 1` (see
  [BENCHMARKING.md](BENCHMARKING.md#coding-tasks-and-the-sandbox)). `vote`, `best_of`, `digest`, `union`
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
- **`skip_aggregator`** (an `llm` aggregator only) applies the cascade's test to the whole panel:
  when at least two answered, their agreement is at least `agreement_threshold`, nothing is
  disputed and the task is not high risk (unless `escalate_on_high_risk: false`), `use` (`vote` or
  `best_of`; `union` on a lensed run, see [Lenses](#lenses)) builds the answer and the synthesizer
  is not called. `routing.reasons` says
  "Synthesis skipped: ..." or "Synthesis ran: ..." and why. The default `panel-duo` sets 0.6,
  between the 0.2.0 ablation's cascade at 0.5 (not worse than one model) and at 0.7
  (inconclusive); the 0.3.0 study calibrates it.
- **`speed`**: `balanced` (default) keeps the configured member order, and a cascade's first wave is
  its cheapest members. `fast` ranks members by measured speed for a typical answer (catalog
  `observed_*`): a cascade's first wave and a panel cut by `max_models` are then the fastest.
- A model may appear only once in a strategy, unless the strategy uses lenses or approaches (see
  below).
- `max_cost_usd` and `max_latency_s` are caps; see [Cost and latency caps](#cost-and-latency-caps).

### Lenses

A **lens** gives a panel member one area of the task to report on, for tasks whose answer is a
list of findings that add up: code review and debugging (plans and architecture decisions have
lenses too, for strategies that synthesize them). Each member still reads the whole input, but its
prompt names its area, what it must leave alone, and the other members' areas, and says that "no
finding" is a complete answer (the answer records `none_found` instead of inventing a point). The
answers are pieces of one result rather than rival answers, which is where a panel of cheap models
gets its diversity now that sampling temperature is gone.

The lenses are in `lenses.yaml` (a config section, `lenses`, so a user config can change one or add
one); the order within a task type is the order they are given out:

| Task type | Lenses |
|-----------|--------|
| `code_review` | `correctness` · `security` (and data handling) · `performance` (concurrency and performance) · `contract` (API contract and tests) |
| `debugging` | `hypotheses` (generation) · `evidence` (reproduction and evidence) · `root_cause` (root cause and minimal fix) |
| `implementation_plan`, `architecture_decision` | `procedure` (steps, order, verification) · `domain` (constraints, failure modes, operations) |

```yaml
lenses:
  migrations:                     # a new lens
    task_types: [code_review]
    instruction: Check schema and data migrations for safety and reversibility.
    owns: [schema changes, data backfills, lock-taking DDL]
    ignores: [style, naming and formatting nits]
```

How lenses are used:

- **`lenses: auto`** (the default for a panel whose aggregator keeps every piece: `llm`, `digest`,
  `union`, `union+check`) gives lenses on a task type that has them; `lenses: off` makes every
  member answer the whole task. `vote`, `best_of` and `verified` keep only what several answers
  share, or one answer, so a strategy that combines them with `lenses: auto` is rejected at load, as
  are a solo strategy and a cascade with lenses.
- **Assignment:** a member that names a `lens` of the task type keeps it; the others take the
  remaining lenses in order. With fewer members than lenses a member holds several (`panel-duo`
  reviews as `correctness+performance` and `security+contract`), so the panel still covers every
  area; with more members than lenses they repeat. One member alone gets no lens.
- **Self-mixtures:** a model listed more than once (only allowed with lenses) is asked once per
  copy, each copy with a different lens: `panel-luna-lenses` asks GPT-6 Luna three times on a
  review. Copies run as seats `gpt-luna`, `gpt-luna#2`, `gpt-luna#3` in the run record, priced as
  the model they copy. A task type with fewer lenses asks the model fewer times (once on a task with
  none), and `routing.reasons` says so.
- **What changes in a lensed run:** the panel waits for every member (early return would drop a
  whole area; hedging and fallback still apply, and a model asked in a member's place keeps the
  member's lens); a refinement round keeps each member inside its area and is not skipped for
  agreement; agreement leaves out the points only one lens could see (`lens_exclusive` in the
  agreement report) instead of counting them against the panel; and a `vote` or `best_of` that would
  build the answer (`skip_aggregator`, a cascade's early exit) becomes `union`. `routing.reasons`
  lists the lenses, and the run's `disagreement` records them with the members that found nothing.
- **`union`** (no model call) merges duplicate findings and checks each against the input by string
  match: a finding whose exact quote is in the input, or whose `file:line` falls in a diff hunk, a
  file snippet or a stack-trace frame, is kept; one whose quote is not in the input, or that names a
  file the input never mentions, is dropped and counted; one that quotes nothing checkable (a
  missing test, say) is kept under "not checked". Suggestions and tests are kept unless invented.
  Findings are ranked by severity. **`union+check`** sends only the "not checked" findings to one
  cheap model (`aggregator_model`, at most 400 output tokens); a confirmation counts only with a
  quote that passes the same string match, and a rejection drops the finding.

### Approaches and `select`

Where the answer to a task is **one whole solution** (an implementation plan, an architecture
decision, a `fusion_ask` answer, a patch) the members' answers are rivals, not pieces, so lenses do
not apply. To make the rivals differ, each member gets an **approach** (`approaches.yaml`, config
section `approaches`): a stance to answer the *whole* task from. One cheap call then picks the best
answer instead of a synthesizer rewriting them.

| Task | Approaches |
|------|------------|
| `implementation_plan` | `smallest_change` · `cleanest_design` · `safest_rollout` |
| `architecture_decision` | `evolve_existing` · `purpose_built` · `adopt_standard` (each option argued by its strongest advocate) |
| `ask` (task type `default`) | `direct` · `grounded` · `thorough` |
| any task that expects a patch | `minimal_fix` · `root_cause_fix` (replace the task type's own) |

```yaml
approaches:
  fewest_deps:                    # a new approach
    task_types: [implementation_plan]
    instruction: Plan it with no new dependency; say what that costs.
```

- **Assignment** follows lenses: a member that names an `approach` keeps it, the others take the
  rest in order (they repeat when there are more members), one member alone gets none, and a model
  listed more than once runs as seats, once per approach (`panel-luna-approaches`).
- **`select`** shows the selector the answers as anonymous candidates `c1`..`cN`, in an order
  shuffled by the run id, with no model, vendor or approach. It decides by delivery, conformance,
  fidelity to the supplied material (an invented specific loses; where the material is silent the
  candidate that says so wins), consistency and fitness, and length is not a merit. It answers with
  an id and at most six lines of why in at most 400 output tokens, so the step costs about a tenth
  of an `llm` synthesis. The chosen answer is returned as written; `select+patch` appends up to
  three short points that other candidates covered and it lacks. `routing.reasons` and
  `disagreement.selection` say which answer won, and the selector's call is in the ledger.
- **A choice that is not an id** (or a failed call, or a cap with no room for the call) falls back
  to a vote, or to `best_of` when no point had a majority, with a warning.
- **Lenses and `select` never combine**: a strategy with `lenses: auto` or a member `lens` and a
  select aggregator is rejected at load, with `approaches` on a non-select aggregator.
  `aggregator: auto` is the one strategy that uses both, each where it fits.
- `skip_aggregator` and `judge_feeds_synthesis` need an `llm` aggregator; neither applies to `select`.

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
| `low` | `solo-haiku` |
| `medium` | `auto` |
| `high` | `panel-refine` |
| `local_only` | `panel-local` |

Override a strategy field the same way as any setting, for example
`FUSION__STRATEGIES__PANEL-NANO__AGGREGATOR_MODEL=claude-sonnet` or
`fusion --set strategies.panel-nano.judge=light review-diff ...`. `fusion config validate` checks
that every strategy names catalog models.

The panel is intentionally cheap; a stronger aggregator (`panel-cheap-strong-synth`) is the first
lever when quality matters more than cost. Panel members receive the role prompts in
`src/fusion/orchestration/prompts.py`.

### Difficulty routing (strategy auto)

`strategy: auto` (`kind: auto`, no members) estimates how hard a request is and starts it on a
rung of the escalation ladder; it asks a higher rung only when the answer shows it is not enough,
and a higher rung reuses every answer the lower ones produced (a model is not asked twice). How it
works is in [ARCHITECTURE.md](ARCHITECTURE.md#difficulty-routing-and-the-escalation-ladder); the
settings are the `routing` section of `routing_policies.yaml`. Every threshold is provisional until
the 0.3.0 study calibrates it on the dev split.

```yaml
routing:
  difficulty:
    weights: {task: 0.15, size: 0.15, files: 0.1, hunks: 0.1, languages: 0.05,
              stack_depth: 0.1, error_type: 0.1, risk: 0.2, question: 0.05}
    task_base: {default: 0.1, answer_eval: 0.3, code_review: 0.4, debugging: 0.5,
                implementation_plan: 0.5, architecture_decision: 0.7}
    scales: {size_tokens: 8000, files: 5, hunks: 10, languages: 3, stack_frames: 15}
    easy_below: 0.25             # score below this: easy
    hard_from: 0.45              # score from this on: hard; normal in between
    ambiguity_margin: 0.05       # this close to a threshold: one classifier call decides
    classifier_model: claude-haiku   # null: never call, keep the signals' level
    classifier_max_tokens: 150
  escalation:
    rungs:                       # cheapest first; each names a solo or panel strategy
      - {name: nano-solo, strategy: solo-haiku-check}
      - {name: nano-panel, strategy: panel-nano-auto}
      - {name: mid-panel, strategy: panel-mid}
      - {name: frontier, strategy: solo-opus, frontier: true}
    start_by_level: {easy: 0, normal: 1, hard: 2}
    allow_frontier: false        # frontier rungs run only when true
    max_rungs: 3                 # rungs one run may try, the start rung included
    triggers:
      self_confidence_below: 0.7 # a single model's own confidence
      agreement_below: 0.5       # tau, for panels without lenses or approaches
```

- **Opting into the frontier rung:** `strategy: auto-frontier` for one call, or
  `routing.escalation.allow_frontier: true` for every `auto` run. A `kind: auto` strategy may set
  `escalation: {allow_frontier: true, max_rungs: 4}` for itself; everything else about answering
  (members, aggregator, rounds, judge, caps other than `max_cost_usd`/`max_latency_s`) belongs to
  the rungs' strategies and is refused on an `auto` strategy.
- **Caps and time:** `max_cost_usd` (the call's or the strategy's) is checked before every rung,
  and so is the soft time limit (`FUSION_TOOL_SOFT_TIMEOUT_S`). When the next rung would pass
  either, or `max_rungs` is reached, or the next rung is a frontier rung that is not allowed, the
  run returns the best answer so far with a warning that says why.
- **What you see:** the response's `escalation` field and its **Route** section say which rung
  answered and why (`answered at rung 1 (nano-panel): panel agreed (0.86)`); `detail: full` and
  `fusion://runs/{run_id}` list every rung tried with its cost, time and trigger.
- **Environment overrides** work as for any setting, for example
  `FUSION__ROUTING__ESCALATION__ALLOW_FRONTIER=true` or
  `FUSION__ROUTING__DIFFICULTY__CLASSIFIER_MODEL=gpt-luna`.

### Upgrading from v0.1.0

`policies.<task>.panel_models`, `max_panel_size`, `high_risk_*`, `budgets` and `synthesizer_model`, and
`refinement.enabled_budgets` and `max_rounds`, are gone; a config that still sets them fails with a
message naming the key. Move the panel into a strategy and set `rounds` for refinement. The
per-task choices went away with them: code review no longer swaps in the security-focused GPT-6 Luna
(`gpt-luna-security` remains in the catalog; give a member `role: security_reviewer` to get the same
prompt for every task) and no longer widens the panel for high-risk diffs.

## Patch verification

The `verify` section sets where `fusion_propose_patch` (`fusion patch`) runs the panel's checks and
your `tests_cmd` ([INTEGRATIONS.md](INTEGRATIONS.md#patches); the security
model is in [SECURITY.md](../SECURITY.md#patch-verification)).

```yaml
verify:
  sandbox_enabled: true      # false: Fusion never runs code; verify=sandbox falls back to host
  check_timeout_s: 10        # each panel-written check
  tests_timeout_s: 120       # your tests_cmd
  mem_mb: 1024               # Linux only (macOS ignores memory limits)
  workers: 2                 # sandbox copies running at once
  max_copy_files: 5000       # a larger project is not copied: verify falls back to host
  max_copy_bytes: 67108864   # 64 MB
  max_file_bytes: 4194304    # 4 MB; a larger file is left out of the copy
```

`FUSION__VERIFY__SANDBOX_ENABLED=false` (or `fusion --set verify.sandbox_enabled=false`) turns
execution off for one shell or one command.

## Modes

A run is either `real` (the default; serving Claude Code) or `benchmark` (measured in a study). The
mode is an argument of `Pipeline.run(ctx, mode=...)`, not a global.

| | `real` | `benchmark` |
|--|--------|-------------|
| Secret redaction | on | off, so ground-truth tasks reach the models as written; a study can turn it on |
| Judge | the strategy's `judge` (default `off`) | the strategy's `judge` |
| Sampling | provider defaults | temperature `0` and seed `0` (a study sets its own seed per repeat) unless a member sets its own temperature; both reach only models whose catalog entry sets `supports_sampling_params` (the local Ollama and LM Studio entries; no packaged cloud model since 0.3.0) |
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
  early_return: {quorum: 2, grace_ms: auto, max_grace_ms: 1000}   # `false` switches it off
  hedge_after_ms: 15000                                           # `false` switches it off
  prompt_cache: auto                                              # auto | always | off
```

`max_concurrency` is per provider: one provider's calls queue behind each other, other providers'
do not. The latency controls are on by default (a policy file that leaves one out has it off):

- `early_return`: once `quorum` members have answered (and at least `min_successful_responses`),
  wait a grace for the rest, then cancel them and carry on. `grace_ms` is a number of milliseconds
  or `auto`: the catalog's measured speed of each straggler (`observed_ttft_ms` plus the answered
  members' output length at `observed_decode_tps`) says when it should finish, and the run waits
  for the next one only if that is within `max_grace_ms`; it does not wait for a straggler that is
  overdue or due later. With no measured straggler, `auto` waits `max_grace_ms`. The run warns
  which models were dropped. A cancelled call may still be billed, so its cost is recorded as
  unknown. A lensed panel does not return early: each member covers an area no other one does
  (see [Lenses](#lenses)).
- `hedge_after_ms`: when a member has not answered after this long, also ask a spare: an enabled
  `panel`-role model of the same price tier that is not in the strategy, another vendor first,
  then the fastest by measured speed. Whichever of the two answers first is used, attributed to
  the model that actually answered, and the other is cancelled. With no spare the run says so and
  keeps waiting.
- **Same-tier fallback** (always on): a member whose call fails because the model is unknown or
  retired (HTTP 404/410), or its provider returns 5xx, rate-limits or cannot be reached after the
  adapter's retries, is asked of a spare chosen the same way, once. The run says which model
  stood in ("Panel model X failed; asked Y (same tier: nano) instead"). Bad requests, auth errors
  and timeouts are not retried on another model.
- `prompt_cache`: every panel member gets the same system prompt and a user prompt that starts
  with the file snippets (the request, context and member role come after), so a later call about
  the same files starts with the same text. OpenAI and Google cache that by themselves. For
  Anthropic models (called directly or through OpenRouter), `auto` adds a cache breakpoint after
  the files when files were given, `always` on every panel call, `off` never; a cache write costs more than plain input (Haiku 5.5: $0.125
  against $0.10 per 1M tokens) and a read much less ($0.01). Cached input tokens are recorded per
  call and priced at the cached rate.

A strategy may set `early_return` and `hedge_after_ms` for itself with `fanout:` (the other limits
stay the routing policy's), and `off` switches one off for that strategy, which is how the latency
benchmark compares them on one panel.

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
