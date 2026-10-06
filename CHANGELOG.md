# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - Unreleased

Evidence-backed release: benchmark mode, a faster and cheaper real mode, latest models, and
one-command integrations. The work is planned task by task in
[docs/superpowers/plans/2026-10-05-v0.2.0-roadmap.md](docs/superpowers/plans/2026-10-05-v0.2.0-roadmap.md).
Add each user-visible change below under the matching heading as its task lands.

### Added

- **Benchmark framework (`fusion bench`).** `fusion bench run --dataset D --arms a,b,c --repeats 3 --max-usd 5 [--mock]` runs every (task, arm, repeat) of a ground-truth dataset as a resumable, concurrent job and records the answer, the claims, the full call ledger and a measured metrics block (seconds to complete, cost, tokens, effective and per-model tokens per second, time to first token, calls, retries, quality, solved). `fusion bench plan` estimates cost and time as a range before spending anything and suggests the largest study that fits `--max-usd`; a live run will not start without a passing plan unless `--yes`. `list`, `show` and `resume` inspect and continue runs; results are kept in `bench-results/` (git-ignored; `FUSION_BENCH_DIR` moves it). See [docs/BENCHMARKING.md](docs/BENCHMARKING.md#benchmark-mode-fusion-bench).
- **Live-spend ledger and caps.** Every live benchmark call is appended to `bench-results/spend.json`, no job starts that would pass the roadmap's $20 total, and `--max-usd` stops a run cleanly (status `stopped`, resumable) when the next job's worst case would not fit. `fusion bench spend` shows the ledger.
- **Simulated models** (`--mock`): a study runs the real pipeline, strategies, catalog models and prices on `SimulatedProvider`s with a configured skill, price, speed and correlated mistakes, deterministic by seed, on a virtual-time event loop, so a study is free, instant, repeatable and needs no API keys. Its skill numbers are assumptions, not measurements.
- **Scorers and judges.** Every benchmark category has a ground-truth scorer: `ReviewScorer` (recall and precision against seeded bugs matched on file, line and category, penalising false positives and hallucinated files), `DebugScorer` (root-cause rank and fix keywords) and `RubricScorer` (a checklist whose judged items must quote the answer, with gating items), each calling an LLM judge only for what the deterministic checks leave open. `PairwiseJudge` compares two answers in both orderings, cancels position bias, and refuses judges from a provider that serves either arm. Scorer cost is `eval_cost_usd` and is priced by `bench plan`. See [docs/BENCHMARKING.md](docs/BENCHMARKING.md#scoring).
- `fusion bench calibrate-judge`: runs judges over seeded good-versus-flawed answers and reports accuracy, tie and position-flip rates, Cohen's κ and agreement between judges; reports are stored in `bench-results/calibration/`, and live runs are forecast, capped and recorded in the spend ledger.
- **Benchmark dataset v1** (`evals/datasets/v1/`): 100 synthetic tasks with ground truth, 25 each of code review (seeded defects with file, line, category and severity, and six clean changes), debugging (trace, logs, code, root cause and fix), architecture decisions and implementation planning (checklist rubrics with required and forbidden points), in Python, TypeScript and Go, easy to hard, split into `dev` (tuning) and `test` (final study only). The tasks are written by an LLM and not yet reviewed by a person; see [evals/datasets/README.md](evals/datasets/README.md) for what that means. `--split dev|test|all` (default `dev`) selects the split.
- **Executable coding tasks** (`evals/datasets/v1/coding/`, benchmark Dataset B): 33 self-contained Python tasks (16 implementations from a spec, 17 bug fixes, one to three files, easy to hard, split into `dev` and `test`) with **hidden tests**; a task's `truth` is the test command, the hidden files and the ids of the tests a correct patch passes. Each ships a reference solution and two plausible wrong fixes, and `fusion bench dataset validate` proves the tests tell them apart by running them. Arms answer with **one patch** (a unified diff or complete files) in the new `patch` field of `PanelAnswer`; the `llm` aggregator is asked to merge the panel's patches into one, and `vote` and a cascade's early exit return the patch most models gave. `CodingScorer` applies the patch tolerantly (hunk numbers are not trusted; unsafe paths are rejected), copies the hidden tests over the result and scores pass@1, with a rerun-twice flake guard; a coding task counts as solved only when every hidden test passes. `--dataset coding` and `--dataset v1` resolve by name; the dataset loader reads a directory holding `task.yaml` as one task. See [docs/BENCHMARKING.md](docs/BENCHMARKING.md#coding-tasks-and-the-sandbox).
- **`Sandbox`** (`fusion.bench.sandbox`): a reusable context manager (`copy_in`, `run`, `collect`) that runs benchmark code in a temporary directory with a wall-clock kill, resource limits, capped output, a scrubbed environment (no API keys) and, where the platform offers it, no network and no writes outside the sandbox (`sandbox-exec` on macOS, `unshare` on Linux). `FUSION_SANDBOX_ISOLATION=auto|require|off`. Benchmark-only and never reachable through MCP; see [SECURITY.md](SECURITY.md#benchmark-sandbox).
- **`best-of-n-verified`** strategy (`aggregator: verified`): the cheap panel each write a patch and the task's visible tests pick the best, with no synthesis call. It runs code, so the pipeline refuses it outside benchmark mode (`BenchmarkOnlyError`) and `fusion_ask` and the other MCP tools return that error.
- **Artifact evaluators and an agentic judge** (benchmark Task 17). Frontend and performance tasks are scored on what the answer *builds*: `bench/evaluators/` measures a copy of the answer's files (hidden tests, build, ruff and complexity and secrets, diff size, a timed benchmark against the reference solution with median, p90, noise, peak memory and a scaling exponent, headless-Chromium screenshots at 390x844 and 1440x900, console errors, accessibility) and returns `Evidence` with ids; hard gates and weighted soft criteria turn it into a completion score (0 if a gate fails). With `--judge-models` an LLM judge inspects the outputs through read-only tools (`list_files`, `read_file`, `grep`, `view_screenshot`, `get_evidence`, `run_evaluator`, `submit_verdict`) under a step cap and a money cap, blind (random A/B labels, both orderings), cross-family, at least two judges, with model output inside `<untrusted>` delimiters, and every tool call is stored with the item. Noisy timings are marked unstable and left unscored; a missing browser makes the browser evidence skipped, not failed. The browser evaluators are the optional extra `fusion-code-orchestrator[bench-visual]` (Playwright). See [docs/BENCHMARKING.md](docs/BENCHMARKING.md#frontend-and-performance-tasks-evidence-and-the-agentic-judge) and [SECURITY.md](SECURITY.md#benchmark-sandbox).
- **Benchmark Dataset C** (`evals/datasets/v1/frontend/`, `performance/`): 12 frontend tasks (HTML, CSS, JavaScript; structural, accessibility and responsiveness tests) and 12 performance tasks (slow Python, hidden behaviour tests, a hidden benchmark and a reference timing), each with a reference solution and two flaws, easy to hard, split into `dev` and `test`. `fusion bench dataset validate` proves them sound by measuring them (a performance task's starter must pass its tests and fail its benchmark); `v1` is now 157 tasks. Synthetic and not yet reviewed by a person; see [evals/datasets/README.md](evals/datasets/README.md#dataset-c-frontend-and-performance-tasks).
- `fusion bench calibrate-judge --artifacts`: calibrates the agentic judge on each task's reference solution against its deliberately broken pages and known-slow or wrong code (accuracy, ties, position flips, κ, accuracy per set). A study whose judges scored an artifact gets **no headline verdict** unless each judge's latest calibration reaches the accuracy floor (default 0.8, `FUSION_JUDGE_ACCURACY_FLOOR`); `bench run` and `bench show` say so and `show --json` has `judge_gate`.
- `fusion bench show RUN --task ID` prints a task's gates, criteria, evidence and the judges' reasoning; `show` and `run` list `eval_cost_usd` and `eval_seconds` (what measuring and judging cost) beside, never inside, the arm's cost and time.
- `fusion bench dataset validate | stats | build`: validate a dataset (schema, unique ids, line numbers inside the supplied files, secrets, split disjointness, a licence note, that each task's truth is scorable offline, and with `--release` the published coverage), print its statistics, compile the authoring YAML into the JSONL (`--check` for drift), or have a model draft candidate tasks (`--generate`, spend-capped).
- Disk response cache for live studies: a repeated request is replayed at zero cost, flagged `cache_hit` on its ledger record and `latency_valid: false` on its item. `ModelResponse.cache_hit` and `CallRecord.cache_hit` are new fields.
- `Pipeline.run` accepts `seed`, `redact` and `ledger`; `PipelineResult.halt_reason` says why a run stopped early; database migration 5 adds the `bench_runs` and `bench_items` tables.

- **Latency work.** A run's wall time is now its critical path: the judge's per-answer calls run together and alongside synthesis (a strategy's `judge_feeds_synthesis: true` restores waiting), the shadow baseline starts with the panel instead of after it, and a refinement round is skipped when the panel already agrees (`refinement.skip_above_agreement`, default 0.8, `null` to always refine). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#concurrency-and-latency).
- `fanout.early_return` (`quorum`, `grace_ms`) stops waiting for stragglers shortly after quorum and cancels them, and `fanout.hedge_after_ms` asks another panel model when one is slow. Both are off by default. Cancelled calls are recorded with unknown cost.
- `RunLedger.timeline()` returns every call as a start/end span on the run's clock.
- **Structured panel outputs:** panelists answer with typed claims (`PanelAnswer`: a summary and atomic `Claim`s with kind, severity, file, line and evidence). Every panel and refinement call sends the schema as its `response_schema` (native structured output where the catalog supports it, spelled out in the prompt otherwise), and the panel prompt now says what a claim is. Previously the prompt asked for "valid JSON matching the requested schema" without ever providing one. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#claims-and-agreement).
- **Deterministic agreement:** `ClaimsStage` clusters claims across models (same file within 3 lines, same kind, or similar wording) and reports consensus, unique and contradicted points, outlier models and an agreement score, with no model call. Tool outputs gain `claims` and `agreement`.
- **Calibrated confidence:** `confidence` is now `0.5 * agreement + 0.3 * evidence + 0.2 * coverage`, less a penalty for disputed points, instead of keyword matching on the answer. It is capped at 0.50 when fewer than two models answered, and the run then carries a low-information warning.
- `detail: "compact" | "full"` on the five orchestration tools. Compact (the default) returns the answer, the top five claims, confidence and one cost line.
- **Strategies:** a strategy is declarative data (`strategies` config section, packaged in `strategies.yaml`) that says which catalog models answer, how many rounds, who aggregates (`llm` or `digest`) and whether a judge scores the answers (`off`/`light`/`full`). Nine ship: `solo-frontier`, `solo-sol`, `solo-cheap`, `solo-luna`, `panel-cheap` (the default), `panel-cheap-strong-synth`, `panel-refine`, `panel-digest` and `panel-local`. Every `fusion_*` tool and the CLI (`--strategy`) accept one, `routing.strategy` reports which ran, and `fusion strategies list` prints them. Solo strategies are one provider call, which is what benchmark baselines use. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#strategies-and-budgets).
- **Run modes:** `Pipeline.run(ctx, mode=Mode.REAL | Mode.BENCHMARK)`. Benchmark mode fixes temperature and seed, sends prompts untrimmed, never runs the shadow baseline and omits the lifetime footer. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#modes).
- `ModelRequest.seed`, sent to OpenAI, Google and Ollama where the model accepts sampling parameters.
- **Cost-optimal aggregation.** `kind: cascade` strategies ask the `cascade.first` cheapest members first (by catalog price) and return at once, with no synthesis and no further model, when their agreement reaches `cascade.agreement_threshold` (provisional 0.7, tuned by the benchmark study), no point is disputed and the task is not high risk; otherwise the rest of the panel answers and the strategy's `rounds` and `aggregator` apply. New `panel-cascade` strategy. `routing.reasons` and the stored run's `cascade` say which way it went. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#strategies-and-budgets).
- Aggregators that make no model call: `vote` (only the points a majority of models backed), `best_of` (the answer the other models' claims back most) and the existing `digest`, where Claude Code is the aggregator. New `panel-vote` strategy. Tool descriptions and server instructions now tell the caller how to consume a digest.
- **Hard cost caps.** `max_cost_usd` on a strategy is enforced: before any call the run is forecast from the catalog and shifted down until it fits (drop refinement and judge calls, then the dearest members, then the cheapest model alone), refused with no spend if even that is over, and during the run stages that would pass the cap (refinement, the judge, synthesis, a cascade's escalation) are skipped with a warning, so a run degrades to a panel digest instead of overspending. `max_latency_s` skips stages that would not fit. The result carries a `budget` record. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#cost-and-latency-caps).
- Optional response cache (`cache.enabled`, `ttl_seconds`, `max_entries`; off by default): a real-mode request identical to a recent one returns its earlier answer with no model call. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#response-cache).
- The strategy cost table in [docs/COSTS.md](docs/COSTS.md#what-a-task-costs-by-strategy) is generated from the catalog by `evals/runners/cost_table.py`, and a test fails when it drifts.
- **Cost ledger:** every LLM call (panel, refinement, judge, synthesis, shadow) is recorded once in a per-run `RunLedger` with tokens, cost, latency, start offset and speed metrics; totals and per-task metrics (`seconds_to_complete`, cost, tokens, effective output tokens/s, critical path) come from it, and the full ledger is stored with each run. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#cost-ledger).
- `ModelEntry.persona` in the catalog (security-focused and deliberately weak panel members), and catalog roles drive model fallbacks.
- **Layered configuration:** packaged defaults, then `config.yaml` in the user config directory, then `./.fusion/config.yaml`, then `FUSION__SECTION__KEY` environment variables, then `fusion --set key=value`. Any of `models`, `provider_limits`, `policies`, `budgets`, `fanout`, `refinement` and `baselines` can be overridden or merged, so an installed copy can be customised without editing the package. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#layers-and-locations).
- `fusion init` (commented starter config, next steps, optional `--import-legacy`), `fusion config show [--resolved] [--json] [--filter]` (every effective value with the layer that set it) and `fusion config paths`.
- Configuration errors are reported as key, value, source layer and rule instead of a traceback; misspelled sections get a suggestion.
- Database migration 4 (index on `runs.created_at`).
- Provider features on `ModelRequest`: `response_schema` (native structured output with a prompt-and-scrape fallback), `reasoning_effort`, `cache_prefix` (Anthropic prompt caching), `stream` (SSE for all three cloud providers), `images` (vision input, guarded by the catalog's `supports_vision`) and `thinking_budget_tokens` for legacy thinking models.
- Speed metrics on every `ModelResponse`: `total_tokens_per_s`, and for streamed calls `ttft_ms` and `decode_tokens_per_s`; plus `cache_write_tokens` and `retries`.
- Per-provider rate limiting (`provider_limits` in the catalog: max in-flight requests and requests per minute); see [docs/CONFIGURATION.md](docs/CONFIGURATION.md#rate-limits-and-reasoning-effort).
- `httpx[http2]` dependency (adds `h2`).
- **Model catalog** (`src/fusion/config/catalog.yaml`): one source of truth for model IDs, capabilities and prices, verified against official provider docs on 2026-10-05. Prices carry `verified_on` and `source_url` and support effective dates (the Gemini 3.8 Flash introductory price ends 2026-12-31 and doubles from 2027-01-01).
- `fusion models list` and `fusion models check [--live]` to inspect the catalog and flag stale prices, upcoming price changes and retiring models; `--live` verifies IDs through the providers' free list-models endpoints.
- A second configurable baseline (GPT-6.1 Sol); `baseline.yaml` now lists baselines as catalog aliases.
- `CHANGELOG.md`, `CLAUDE.md` (agent/contributor guide), GitHub Actions CI, issue and pull request templates.
- `docs/CONFIGURATION.md`, `docs/INTEGRATIONS.md` and `docs/BENCHMARKING.md`; a docs test that checks relative links and anchors, the canonical docs, and the README size limit.
- In-process FastMCP smoke tests (tool registration, a `fusion_ask` round trip, and a guard that mock mode never reaches real providers).
- Version parity test between `pyproject.toml` and `plugin/plugin.json`; `pytest` marker `live` for tests that call real provider APIs (skipped by default).

### Changed

- **Benchmark mode streams every call and no longer redacts secrets** (it fixes the sampling seed per repeat, as before). Streaming measures time to first token and decode speed; redaction would alter ground-truth tasks that contain secret-looking text on purpose, and `redact: true` turns it back on. Real mode is unchanged. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#modes).
- `max_cost_usd` and `max_latency_s` on a strategy were only checked after a run; they now stop spending before and during it (see Added). A run that still ends over a cap adds a warning.
- `fanout_to_panel` accepts `min_successful`, replacing the configured quorum for one call.
- `fanout.max_concurrency` now caps in-flight calls per provider rather than across the whole panel, so a slow provider no longer holds up calls to others.
- Fusion's reported wall time no longer includes the shadow baseline: it is stamped when Fusion's answer is ready, and the shadow call runs alongside it instead of after it. A request with shadow enabled still returns only once the comparison is done.
- Per-answer judge calls were sequential and are now concurrent; the `judge_*` records in the ledger keep their order.
- **Confidence values change.** Reported confidence used to be 0.5 or 0.7 depending on whether the answer contained words like "risk" or "confidence", and it ignored how much the models agreed. It now follows agreement, evidence and coverage, so typical three-model runs report lower, more varied numbers (and one-model runs at most 0.50).
- **`display_markdown` is compact by default and no longer cut at 1,200 characters.** The compact form shows the answer's own summary rather than the synthesizer's raw JSON; `detail: full` restores the cost breakdown, shadow lines, lifetime footer and every warning.
- Per-answer scores without a judge are measured from claims (share citing a file, carrying evidence, proposing an action) instead of response length and Markdown markers; unmeasurable dimensions are a neutral 0.5. The final evaluation's usefulness, readiness, test-plan and residual-risk numbers are derived from claim clusters. With no claims (a halted run) they are 0.
- Deterministic checks no longer flag missing Markdown layout on claims JSON, check cited files from the claims themselves, and require a `test` claim (not the word "test") in coding answers.
- The synthesizer prompt receives claim clusters and readable answers instead of word-overlap bullets. For `solo` runs, `panel-digest` and non-JSON synthesis, task fields such as `critical_findings` and `ranked_hypotheses` are filled from the clusters.
- A `solo` run's `final_answer` is the answer rendered as Markdown, and the `panel-digest` document lists shared, disputed and single-model points before each answer.
- `disagreement_score` is `1 - agreement` (0 with fewer than two models, flagged by `low_information`); `consensus` now needs an agreement score of at least 0.5 and no disputed severity.
- **Which models run is now chosen by a strategy, and the default changed.** The legacy `budget` argument still works and maps to strategies: `low` → `solo-cheap` (one Haiku call, previously a Gemini panel of one plus synthesis and judging), `medium` → `panel-cheap` (Haiku 4.5, GPT-6 Luna and Gemini 3.8 Flash for every task, merged by **Haiku 4.5** instead of Sonnet 5.5; use `panel-cheap-strong-synth` for Sonnet), `high` → `panel-refine`, `local_only` → `panel-local`.
- **The LLM judge is off by default.** Runs no longer make a judge call per answer; scoring falls back to deterministic checks and heuristics, so a run costs only its panel and aggregator. Set a strategy's `judge` to `light` or `full` to restore it.
- `policies.<task>` now holds only `judge_model` and `min_context_score`, and `refinement` only timeouts and `min_panel_size`; refinement rounds come from a strategy's `rounds`. A config that still sets `panel_models`, `max_panel_size`, `high_risk_*`, `budgets`, `synthesizer_model`, `enabled_budgets` or `max_rounds` fails with a message naming the key. Code review no longer swaps in the security-focused GPT-6 Luna or widens the panel for high-risk diffs. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md#upgrading-from-v010).
- `Router.route()` takes a `strategy` instead of a `budget`; model filtering by cost tier is gone, so a strategy may name any enabled model.
- Run display text gains a `Strategy:` line.
- **Pipeline refactor:** the 1,500-line `pipelines.py` is split into stages (`Redact`, `Route`, `ContextEval`, `Panel`, `Refine`, `Judge`, `Aggregate`, `FinalEval`, `Shadow`, `Persist`) over one `RunState`, plus `ledger`, `result`, `output`, `pipeline`, `specialized` and `factory` modules; no function in `orchestration/` exceeds 120 lines. `fusion.orchestration.pipelines` still re-exports the public names. `create_pipeline`/`create_pipelines` are deprecated in favour of `build_pipeline(s)(Settings(...), providers)`.
- **Judge and eval calls are now part of the reported cost.** Previously only panel, refinement and synthesis calls were itemized, so reported Fusion cost was understated and `usage.per_model`, `cost_latency.steps` and `usage.total_*_tokens` omitted the judge. They now include one `judge:<model>` step per answer, and the "judge calls are not itemized" warning is gone. A call that fails or times out may still have been billed, so a run with such a call reports its total cost as unknown.
- `usage.total_model_call_latency_ms` is now the sum of all model calls (it previously left out refinement and judge calls).
- Offline (mock) mode is chosen once by the factory and uses its own registry and routing policies instead of `is_test_mode` branches inside the Router; `Router.route()` and `RoutingPolicy.select_*()` no longer take `test_mode`. Offline routing reasons now read like live ones ("Applied medium budget policy").
- Panel members answer with the task's system prompt unless their catalog entry sets a `persona`. Previously a persona was guessed from the model alias and strengths, which gave most cloud panelists an unrelated "implementation planner" prompt on every task.
- Prompts are no longer cut at fixed character limits (2,000-6,000 characters of the task, answers and context were silently dropped). Content is trimmed only when it would exceed the model's catalog `context_window`, and the run then carries a warning naming the model.
- **Run database moved (breaking):** the default is now `runs.db` in the platform user data directory instead of `./fusion_runs.db` in whatever directory started Fusion, so `fusion mcp` no longer creates files in your project. The old file is not read or moved automatically; run `fusion init --import-legacy` once to copy its history, or set `FUSION_DB_PATH` to keep using it. If your `.env` still contains `FUSION_DB_PATH=./fusion_runs.db` (copied from the old `.env.example`), remove that line.
- The plugin's `.mcp` server config no longer pins `FUSION_DB_PATH` to the workspace.
- SQLite: WAL mode, one lock-guarded connection per `RunStore` instead of one per call, writes run in a worker thread from async code, migrations apply atomically, and the lifetime-stats footer queries the database once per run (it was queried twice).
- **Provider layer overhaul:** each provider keeps one pooled HTTP client (HTTP/2 for cloud providers) that is closed on shutdown; transient failures (429, 408, 5xx, timeouts) are retried with full-jitter backoff and `Retry-After`, 4xx errors never are; failures carry a typed `error_type` (`RateLimit`, `Auth`, `Timeout`, `BadRequest`, `Server`, `Connection`) and a retry count. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#providers).
- Token accounting is normalized across providers so costs are correct: Anthropic cache reads/writes are included in `input_tokens` and cache writes are billed at the catalog cache-write price; Gemini thinking tokens are included in `output_tokens`. Costs for runs that use caching or thinking models change accordingly.
- Model parameters now follow the catalog instead of name regexes: OpenAI reasoning models send `max_completion_tokens` and `reasoning_effort`, Gemini sends `thinkingLevel`, Claude sends `output_config.effort`, and sampling parameters are omitted where the catalog says they are unsupported. Panel models (GPT-6 Luna, Gemini 3.8 Flash) default to `low` reasoning effort, and reasoning models request 16384 output tokens so thinking does not truncate answers.
- Gemini requests send the system prompt as `systemInstruction` and keep user/assistant turns separate.
- **Default models updated:** panel = Claude Haiku 4.5 + GPT-6 Luna + Gemini 3.8 Flash, synthesizer = Claude Sonnet 5.5, baseline = Claude Opus 5.5 (was GPT-5.4 mini, Gemini 3.5 Flash, Sonnet 4.6 and Opus 4.8). Aliases changed: `gpt-5.4-mini` is now `gpt-luna`, `gpt-5.4-mini-security` is now `gpt-luna-security`.
- **Pricing corrected:** the previous Gemini price ($0.10/$0.40) belonged to a retired generation, the previous Sonnet and GPT prices were unverified placeholders, and a hard-coded table still priced Opus at $15/$75. Cost reports now use verified list prices, and cached input tokens are billed at the cached rate (or the input rate) instead of being free.
- `fusion compare-cost` and the `evals/runners/compare_cost.py` runner take a catalog alias (`--opus-model claude-opus`). `fusion config validate` checks the catalog, baselines and routing references and prints catalog warnings.
- The Anthropic sampling-parameter guard now covers every Claude 4.7+ model (Opus 5.5, Sonnet 5.5, Fable 5.1), not only `claude-opus-4-N`.
- Dependencies upgraded to the latest releases and the lockfile refreshed, including FastMCP 3.4 → 4.0 (MCP SDK 1.x → 2.x), Typer 0.26 → 0.27, Pydantic 2.13.5, ruff 0.15 → 0.16 and mypy 2.1 → 2.4; minimum versions in `pyproject.toml` now match what is tested (`fastmcp>=4.0`, `httpx>=0.28`, `pydantic>=2.13`, `typer>=0.27`, ...). The suite passes on Python 3.12 and 3.13 with warnings treated as errors.
- Removed the unused `pydantic-settings` dependency; added `platformdirs` (used by the upcoming config/paths work).
- README reduced from 696 to under 200 lines; detail moved to the canonical docs (configuration, integrations, benchmarking, costs, security) with duplicated sections removed.
- `CONTRIBUTING.md` now defines the docs contract, the quality gate, the `live` test marker and the changelog rule; `docs/COSTS.md` no longer repeats price numbers (they live in `pricing.yaml`).
- The package version is read from installed package metadata (`pyproject.toml` is the single source).
- Copyright holder and package author are now Oleksandr Shevchenko.
- Removed the duplicated `dev` optional-dependency block; development tools live in `[dependency-groups]` only.
- `.gitignore` covers `bench-results/` and `.fusion/`.

### Fixed

- A panel call still pending at the global timeout is attributed to its own model; it used to be attributed to the first model that had not finished.
- OpenAI-compatible requests no longer break on models that reject `max_tokens` or `temperature`.
- Errors from providers no longer echo API keys.
- `FUSION_DEFAULT_PROVIDER=mock` is now honored by the MCP tools. Previously the MCP server ignored it and could call real providers when API keys were present.
- README no longer links to a nonexistent publication checklist and its tool table matches the server.
- The disagreement summary listed models in an order that changed between processes (it joined a set); it is now sorted, so identical runs produce identical output.

### Removed

- `evals/disagreement_eval.py` (score variance), `heuristic_judge_scores` (response length and Markdown markers) and the keyword checks for uncertainty words and the word "test"; the word-overlap finding grouping in `disagreement.py`; the Markdown-scraping fallback in `output_parser.py`.
- `RefinementConfig.enabled_budgets`, `RefinementConfig.max_rounds` and `RefinementConfig.enabled_for()` (a strategy's `rounds` replaces them), and `ModelRegistry.filter_candidates()` with `cost_tier_within_budget()`.
- `default_models.yaml`, `pricing.yaml` and the hard-coded `fusion.telemetry.pricing` table (replaced by the catalog), plus the `cost_per_1k_*` model fields and the unused `compute_cost` helper.
- **Legacy agent harness** (breaking): the `fusion_compare_implement` MCP tool, the `fusion compare-implement` CLI command, the `fusion.agent` package, `fusion.benchmark.compare`, and the `FUSION_AGENT_MODE` / `FUSION_WORKSPACE_ROOT` settings. It executed file writes and shell commands for little value and its one real run was inconclusive. The MCP server now exposes eight tools. To compare Fusion against a single model use `fusion_compare_claude_runs`, the shadow A/B, or the `fusion bench` mode coming in this release (see [docs/BENCHMARKING.md](docs/BENCHMARKING.md)). The June 2026 result is archived in `evals/archive/`.

### Security

- The Gemini API key is sent in the `x-goog-api-key` header instead of the URL query string, so it no longer appears in logs or exception text; provider error messages are scrubbed of key-shaped strings.
- `SecurityPolicy` no longer carries file-write or shell-execution switches; Fusion has no code path that writes files or runs commands.

## [0.1.0] - 2026-07-08

First working version: a Python MCP server that gives Claude Code (and Cursor) a cheap
multi-model panel for coding questions, with cost and quality instrumentation.

### Added

- **MCP server** (FastMCP, stdio) with nine tools: `fusion_ask`, `fusion_review_diff`,
  `fusion_debug_error`, `fusion_decide_architecture`, `fusion_plan_feature`,
  `fusion_eval_answer`, `fusion_compare_claude_runs`, `fusion_stats`, and the optional
  `fusion_compare_implement` isolated agent benchmark.
- **Cheap-panel orchestration**: Claude Haiku 4.5, a GPT mini and Gemini Flash answer in
  parallel; a Claude Sonnet synthesizer merges the answers into task-specific structured output.
- **Mixture-of-agents refinement** at `high` budget: each panel model critiques anonymized
  peer answers and revises its own before synthesis; failed refinements keep the round-1 answer.
- **Concurrent fan-out** with per-model and global timeouts, concurrency limit, quorum, and
  partial results.
- **Budget-aware routing** (`low`, `medium`, `high`, `local_only`) from YAML routing policies,
  with task classification, complexity and risk estimation.
- **Hybrid evaluation**: LLM-as-judge scores plus deterministic checks (secret leakage,
  dangerous shell commands, unsupported file references, missing test plan), disagreement
  analysis, and an aggregate confidence.
- **Cost and usage telemetry**: per-call token, latency and cost records, a YAML pricing
  registry, and a baseline comparison against a single frontier model (Opus 4.8).
- **Shadow baseline A/B** (`FUSION_SHADOW_MODE=off|sampled|always`): also calls the real
  baseline and records a blind pairwise judge verdict with actual baseline cost and latency.
- **Cumulative statistics**: `fusion stats`, the `fusion_stats` tool, and a lifetime footer on
  every run's `display_markdown`.
- **Direct provider adapters** for Anthropic, OpenAI and Google, optional Ollama and LM Studio,
  and a deterministic mock provider for offline development and tests.
- **Secret redaction** before provider calls; raw prompt logging is off by default.
- **SQLite run store** with full traces, steps, costs, latencies and shadow comparisons.
- **CLI** (`fusion`): review, debug, decide, plan, eval-answer, compare-claude-runs,
  compare-implement, runs list/show/costs/compare-baseline/export, config validate, stats, mcp.
- **Claude Code plugin** (skills and slash commands) and a Cursor MCP configuration.
- **Optional isolated agent harness** (`FUSION_AGENT_MODE=true`) for Opus-versus-Fusion
  implementation experiments in temporary workspace copies.
- Offline evaluation runners and eight sample cases under `evals/`; 103 offline tests, ruff
  and strict mypy clean.

### Known limitations

- The claim that a cheap panel matches a frontier model is not yet measured against ground truth.
- Quality scores come from LLM-judge output and heuristics, not verified outcomes.
- Judge and eval calls are not included in the reported Fusion cost.

[0.2.0]: https://github.com/alexandre0sheva/fusion-code-orchestrator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/alexandre0sheva/fusion-code-orchestrator/releases/tag/v0.1.0
