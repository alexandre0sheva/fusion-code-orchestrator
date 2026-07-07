# Fusion v2 — Cheap-Panel Orchestrator with Honest Stats

**Date:** 2026-07-08
**Status:** Approved

## Goal

Fusion acts as a "cheap frontier-class model" behind MCP for Claude Code. Claude Code
does all repository reads, edits, and shell commands; Fusion only answers, using an
orchestrated panel of small/cheap models, and proves with honest statistics that it
approaches or beats big-model quality at a fraction of the cost.

## Decisions (user-approved)

1. **Panel composition:** cheap panel (Claude Haiku 4.5, GPT mini, Gemini Flash) with
   **Claude Sonnet 4.6 as synthesizer**. Judge stays on Gemini Flash.
2. **Refinement:** Mixture-of-Agents style refinement round, **budget-gated**
   (enabled at `high` budget by default; `low`/`medium` stay single-round).
3. **Stats/proof:** opt-in **shadow baseline A/B** (real Opus 4.8 call + blind
   pairwise judge) plus a cumulative **stats dashboard** (`fusion stats` CLI and
   `fusion_stats` MCP tool).

## Components

### 1. Model registry, pricing, routing (config)

- Add `claude-haiku` → `claude-haiku-4-5` ($1.00 / $5.00 per 1M tokens) to
  `default_models.yaml` and `pricing.yaml`.
- **Correct Opus 4.8 pricing to $5.00 / $25.00 per 1M tokens** (was $15/$75 —
  overstated baseline savings 3×).
- Repoint `routing_policies.yaml`:
  - Default panels use claude-haiku + gpt-5.4-mini(+security role) + gemini-flash.
  - Synthesizer: claude-sonnet. Judge: gemini-flash.
  - Budgets: `low` = 1 cheap model / no refinement; `medium` = 3-model cheap panel /
    no refinement; `high` = cheap panel + refinement round; `local_only` unchanged.

### 2. Refinement round (`src/fusion/orchestration/refine.py`)

- After round-1 fan-out succeeds (quorum met) and policy enables refinement for the
  effective budget, each *successful* panel model receives the anonymized peer
  answers ("Response A", "Response B", …) plus its own answer, with a
  critique-then-improve prompt, and returns a revised answer.
- Implemented on top of the existing fan-out machinery (per-model timeout, global
  timeout, partial results). Concurrency shared with fanout config.
- Fallback: a model whose refinement call fails keeps its round-1 answer.
  Refinement can degrade to a no-op, never to data loss.
- Telemetry: each call recorded as `refine:{model}` step with tokens/cost/latency;
  usage aggregated into the run's `UsageSummary`.
- Config: `refinement:` block in `routing_policies.yaml`:
  ```yaml
  refinement:
    enabled_budgets: [high]
    per_model_timeout_seconds: 45
    max_rounds: 1
  ```

### 3. Shadow baseline A/B (`src/fusion/benchmark/shadow.py`)

- Trigger: env `FUSION_SHADOW_MODE` = `off` (default) | `sampled` | `always`, plus
  per-call boolean `shadow_baseline` on MCP tool inputs (overrides env in both
  directions). `sampled` uses `FUSION_SHADOW_SAMPLE_RATE` (default 0.2).
- When triggered: the same sanitized task prompt is sent to the configured baseline
  model (Opus 4.8). A **blind pairwise judge** receives both final answers in
  randomized order without labels and returns winner + per-answer scores (JSON).
- Storage: new SQLite table `shadow_comparisons(run_id, baseline_model,
  winner, fusion_score, baseline_score, judge_model, fusion_cost_usd,
  baseline_cost_usd, fusion_latency_ms, baseline_latency_ms, created_at, raw_json)`.
- When a shadow ran, `cost_comparison` reports the **actual** baseline cost and
  latency instead of the estimate, flagged `is_estimate: false`.
- Any shadow failure (baseline error, judge error) degrades to a warning; the main
  run always succeeds independently.

### 4. Stats surface

- `RunStore` gains aggregation queries: totals, per-task-type breakdown, shadow
  win/tie/loss counts, cumulative fusion cost vs baseline estimate and actuals.
- `fusion stats` CLI command renders the dashboard.
- New MCP tool `fusion_stats` returns the same data structured + display_markdown.
- Every pipeline's `display_markdown` gains a lifetime footer:
  `Lifetime: N runs · $X spent vs $Y baseline est. (Z% saved) · shadow win-rate W%`.

### 5. Tests & docs

- Offline mock-provider tests: refinement round (including partial failure
  fallback), shadow comparison (mock baseline + mock judge), stats aggregation,
  config validation for new blocks, pricing entries.
- README + docs/ARCHITECTURE.md updated for panel defaults, refinement, shadow
  mode, and stats.

## Non-goals / invariants

- No change to the MCP security boundary: Fusion stays side-effect-free; no repo
  writes or shell execution in orchestration tools.
- No new providers.
- Existing MCP tool schemas remain backward compatible (only additive fields).

## Error handling summary

| Failure | Behavior |
|---|---|
| Refinement call fails/times out | Keep that model's round-1 answer; warning |
| All refinement calls fail | Proceed with round-1 answers; warning |
| Shadow baseline call fails | Run succeeds; warning; no comparison row |
| Blind judge fails | Run succeeds; warning; comparison row omitted |
| Stats query on empty DB | Zeroed dashboard, no error |
