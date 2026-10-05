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

- **Model catalog** (`src/fusion/config/catalog.yaml`): one source of truth for model IDs, capabilities and prices, verified against official provider docs on 2026-10-05. Prices carry `verified_on` and `source_url` and support effective dates (the Gemini 3.8 Flash introductory price ends 2026-12-31 and doubles from 2027-01-01).
- `fusion models list` and `fusion models check [--live]` to inspect the catalog and flag stale prices, upcoming price changes and retiring models; `--live` verifies IDs through the providers' free list-models endpoints.
- A second configurable baseline (GPT-6.1 Sol); `baseline.yaml` now lists baselines as catalog aliases.
- `CHANGELOG.md`, `CLAUDE.md` (agent/contributor guide), GitHub Actions CI, issue and pull request templates.
- `docs/CONFIGURATION.md`, `docs/INTEGRATIONS.md` and `docs/BENCHMARKING.md`; a docs test that checks relative links and anchors, the canonical docs, and the README size limit.
- In-process FastMCP smoke tests (tool registration, a `fusion_ask` round trip, and a guard that mock mode never reaches real providers).
- Version parity test between `pyproject.toml` and `plugin/plugin.json`; `pytest` marker `live` for tests that call real provider APIs (skipped by default).

### Changed

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

- `FUSION_DEFAULT_PROVIDER=mock` is now honored by the MCP tools. Previously the MCP server ignored it and could call real providers when API keys were present.
- README no longer links to a nonexistent publication checklist and its tool table matches the server.

### Removed

- `default_models.yaml`, `pricing.yaml` and the hard-coded `fusion.telemetry.pricing` table (replaced by the catalog), plus the `cost_per_1k_*` model fields and the unused `compute_cost` helper.
- **Legacy agent harness** (breaking): the `fusion_compare_implement` MCP tool, the `fusion compare-implement` CLI command, the `fusion.agent` package, `fusion.benchmark.compare`, and the `FUSION_AGENT_MODE` / `FUSION_WORKSPACE_ROOT` settings. It executed file writes and shell commands for little value and its one real run was inconclusive. The MCP server now exposes eight tools. To compare Fusion against a single model use `fusion_compare_claude_runs`, the shadow A/B, or the `fusion bench` mode coming in this release (see [docs/BENCHMARKING.md](docs/BENCHMARKING.md)). The June 2026 result is archived in `evals/archive/`.

### Security

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
