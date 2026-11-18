# Costs and Baseline Comparison

Fusion tracks cost and usage at provider-call granularity. The goal is transparent
comparison, not false precision.

Canonical for the cost and pricing methodology: where prices come from, how a call and a run are
priced, what the baseline comparison means, and the per-strategy cost table (generated from the
catalog).

## Pricing: the model catalog

Prices live in the model catalog, `src/fusion/config/catalog.yaml`, next to each model's provider
ID and capabilities. That file is the single source of truth for the numbers; this page does not
repeat them. Run `uv run fusion models list` to see current IDs and prices.

Each model has one or more price schedules (USD per 1M tokens):

- `input_per_1m` and `output_per_1m`;
- optional `cached_input_per_1m`, `cache_write_per_1m` and `reasoning_per_1m` (only when reasoning
  tokens are billed separately from output tokens);
- `effective_from` / `effective_until` for scheduled provider changes, for example an
  introductory price that ends on a known date (the right schedule is chosen by today's date);
- `above_prompt_tokens` for a long-context price (see below);
- `verified_on` and `source_url`: every cloud price must name where and when it was verified.

### Long-context prices

Some models charge more for a large prompt: Claude Haiku 5.5 above 100,000 prompt tokens, and the
GPT-6.x models above 272,000. A price row with `above_prompt_tokens: N` applies to a request whose
prompt (every input token, cached ones included) is larger than `N`, and then to the whole request,
as the providers bill it. Each call is priced on its own prompt size: the ledger uses the call's
reported input tokens, and the `max_cost_usd` forecast uses the planned prompt size (prompt plus
900 tokens of overhead). A Haiku 5.5 call with a 150K prompt is priced at $0.50 / $2.50, not
$0.10 / $0.50.

### Price tiers

A model's tier is computed from its price, never written by hand. The `tiering:` block of
`catalog.yaml` sets the weights and thresholds:

`blended = (3 × input + output) / 4` USD per 1M tokens (a 3:1 input:output mix, typical of review
and debug calls).

| Tier | Blended price | Example |
|---|---|---|
| `nano` | below $0.50 | $0.10 / $0.50 → $0.20 |
| `low` | $0.50 to below $2.50 | $1 / $5 → $2.00 |
| `medium` | $2.50 to below $6 | $2 / $10 → $4.00 |
| `high` | $6 to below $12 | $4 / $20 → $8.00 |
| `premium` | $12 and above | $8 / $40 → $16.00 |

The tier follows the price in effect on a date and for a prompt size, so it moves with a scheduled
price change (Gemini 4 Carbon and Argon go from `medium` to `high` on 2026-12-01; Gemini 3.8 Flash
from `low` to `medium` on 2027-01-01) and with a long prompt (Haiku 5.5 is `low` above 100K).
`fusion models list` shows each model's blended price, tier and coming tier changes
(`--on YYYY-MM-DD` prices the catalog on another date). A model with no price in effect counts as
`premium`.

Local and mock models need no price block and are free. Token accounting rules:
`input_tokens` includes cached tokens, which are billed at the cached rate (or the input rate when
no cached rate is configured); reasoning tokens are assumed to be part of `output_tokens` unless a
separate `reasoning_per_1m` is set.

If provider billing returns an actual cost, Fusion prefers that (OpenRouter does: every call
through it asks for `usage.cost`, which the ledger records as billed, not estimated). Otherwise it estimates from the
catalog price in effect today and the reported token usage. If pricing or token usage is missing,
cost is marked unknown rather than invented.

### Keeping prices honest

```bash
uv run fusion models check          # stale prices, price changes and retirements coming up
uv run fusion models check --live   # also ask each provider's free list-models endpoint, and OpenRouter
```

`check` warns when a price was verified more than 60 days ago, when a price changes within 30 days
(with the tier change, if any), when an enabled model is not the newest of its family in the
catalog, and when a provider's earliest retirement date for a model is within 60 days. `--live`
sends your API keys (in headers) to the providers' free `models` endpoints only, and also flags an
ID the provider does not list, a family where the provider lists a newer model (Flash-Lite and
other suffixed variants count as other families), and a price `source_url` that redirects. It also
reads OpenRouter's public model list (no key) and, for every model with an OpenRouter ID, flags an
ID OpenRouter does not list (`UNKNOWN`), a price more than 5% away from OpenRouter's in either
direction, long-context and time-of-day rates included (`DRIFT`), and a newer model of the same
family, by version and listing date (`NEWER`); and it reads OpenRouter's provider list for hosts
that may train on prompts missing from `openrouter.provider_ignore` (`POLICY`). The vendors'
list-models endpoints carry no prices, so prices are compared with OpenRouter only. It
makes no completion calls and costs nothing. `fusion doctor` shows the same warnings (the live ones
with `--live`).

## Baseline models

Baselines are configured in `src/fusion/config/baseline.yaml` as catalog aliases:

```yaml
baselines:
  - name: "Opus 5.5"
    model: claude-opus
    enabled: true
    estimate_strategy: "same_input_and_output_tokens"
```

The provider, model ID and price are resolved from the catalog. The first enabled baseline is the
one used for cost comparison and the shadow A/B; further entries are extra comparison arms for the
benchmark mode (GPT-6.2 Sol, and the benchmark-only Claude Fable 5.2, GPT-6.2 Astra and Gemini 4
Carbon). The default strategy estimates the cost of sending the same aggregate input/output
token volume to the baseline model. This is useful for directional comparison, but it is not the
same as actually running the baseline model.

## What a task costs, by strategy

The synthesis call reads every answer, so it is a large share of a panel's cost (about half of
`panel-nano`, most of `panel-cheap-strong-synth`). That is why strategies can drop it (`digest`,
`vote`, `best_of`) or skip it on easy tasks (`cascade`). The table below prices
each packaged strategy for a typical task: every call reads 5,000 tokens (the task plus system
prompt and schema) and every answer is 1,500 tokens, at the catalog prices in effect on the date
below. It is generated by the same arithmetic the cost cap uses (`fusion.routing.budget`), not
typed by hand, and `tests/test_cost_table.py` fails when it drifts from the catalog.

<!-- cost-table:start as-of=2026-11-18 -->
| Strategy | Calls | Cost per task | Relative to `solo-opus` |
|----------|-------|---------------|----------------------|
| `auto (answered at rung 0, nano-solo)` | 1 | $0.0013 | 0.02x |
| `auto-frontier (answered at rung 0, nano-solo)` | 1 | $0.0013 | 0.02x |
| `solo-haiku` | 1 | $0.0013 | 0.02x |
| `solo-haiku-check` | 1 | $0.0013 | 0.02x |
| `solo-luna` | 1 | $0.0013 | 0.02x |
| `panel-cascade (first wave agrees)` | 2 | $0.0025 | 0.05x |
| `panel-duo (panel agrees)` | 2 | $0.0026 | 0.05x |
| `panel-haiku-lenses` | 3 | $0.0039 | 0.08x |
| `panel-luna-lenses` | 3 | $0.0039 | 0.08x |
| `panel-duo (synthesizes)` | 3 | $0.0043 | 0.09x |
| `panel-luna-approaches` | 4 | $0.0050 | 0.10x |
| `panel-mixed` | 4 | $0.0055 | 0.11x |
| `panel-open` | 5 | $0.0089 | 0.18x |
| `solo-flash` | 1 | $0.0094 | 0.19x |
| `best-of-n-verified` | 3 | $0.0119 | 0.24x |
| `panel-vote` | 3 | $0.0119 | 0.24x |
| `panel-digest` | 3 | $0.0124 | 0.25x |
| `panel-nano-union` | 3 | $0.0124 | 0.25x |
| `solo-terra` | 1 | $0.0125 | 0.25x |
| `panel-nano-select` | 4 | $0.0133 | 0.27x |
| `verified-synth` | 4 | $0.0133 | 0.27x |
| `auto (answered at rung 1, nano-panel)` | 4 | $0.0144 | 0.29x |
| `auto-frontier (answered at rung 1, nano-panel)` | 4 | $0.0144 | 0.29x |
| `panel-nano` | 4 | $0.0144 | 0.29x |
| `panel-nano-auto` | 4 | $0.0144 | 0.29x |
| `solo-hydrogen` | 1 | $0.0187 | 0.37x |
| `solo-argon` | 1 | $0.0250 | 0.50x |
| `solo-carbon` | 1 | $0.0250 | 0.50x |
| `solo-sol` | 1 | $0.0250 | 0.50x |
| `solo-sonnet` | 1 | $0.0250 | 0.50x |
| `panel-refine` | 7 | $0.0297 | 0.59x |
| `auto-frontier (answered at rung 3, frontier)` | 1 | $0.0500 | 1.00x |
| `solo-opus` | 1 | $0.0500 | 1.00x |
| `panel-cascade (escalates)` | 4 | $0.0504 | 1.01x |
| `panel-cheap-strong-synth` | 4 | $0.0509 | 1.02x |
| `auto (answered at rung 2, mid-panel)` | 4 | $0.0975 | 1.95x |
| `auto-frontier (answered at rung 2, mid-panel)` | 4 | $0.0975 | 1.95x |
| `panel-mid` | 4 | $0.0975 | 1.95x |
| `solo-astra` | 1 | $0.1000 | 2.00x |
| `solo-fable` | 1 | $0.1000 | 2.00x |
| `auto (climbs rungs 0-2, at most)` | 9 | $0.1131 | 2.26x |
| `auto-frontier (climbs rungs 0-2, at most)` | 9 | $0.1131 | 2.26x |
<!-- cost-table:end -->

How to read it: `auto` has a row per rung that can answer (where an easy, normal or hard request
starts and ends) and one for the longest climb a run may make; a real climb costs less than that
row, because a higher rung reuses the answers below it instead of asking those models again. A
refinement round re-reads the peers' answers, a synthesizer reads every answer
plus the claim clusters, and the cascade rows are its two outcomes (the two cheapest models agree
and the run stops, or it escalates to the whole panel and the synthesizer). The numbers are an
illustration of how strategies compare, not a measurement: real answer lengths differ, reasoning
tokens are billed as output, and quality is not in the table (the benchmark mode measures it).
Local models are free and left out.

Regenerate after a catalog or strategy change (or to price another date):

```bash
uv run python evals/runners/cost_table.py --write              # rewrite the table in this file
uv run python evals/runners/cost_table.py --on 2027-01-01      # print it for another date
```

### Cost caps

A strategy's `max_cost_usd` is a hard cap enforced twice (settings in
[CONFIGURATION.md](CONFIGURATION.md#cost-and-latency-caps)). Before any call, the run's planned
calls are priced with the assumptions above, using the prompt's real size, and the strategy is
shifted down until the forecast fits; a forecast that cannot fit even the cheapest model alone
refuses the run with no spend. During the run, each optional stage is priced against the money
already spent, using the answers' real sizes, and skipped if it would pass the cap. The forecast
is an estimate: a run's reported cost always comes from the ledger, and a run that still ends over
its cap says so in its warnings.

## What the comparison reports

`CostComparison` includes:

- baseline name and model ID;
- Fusion total cost;
- baseline estimated cost;
- savings in USD;
- savings percent;
- whether Fusion was cheaper;
- whether both costs are known;
- notes explaining estimate assumptions.

`UsageSummary` includes:

- total input/output/token counts;
- per-model usage and failures;
- Fusion wall latency;
- panel wall latency;
- synthesis latency;
- summed model-call latency;
- max panel latency.

## What Claude Code sees

Every orchestration response carries a compact cost section in `display_markdown` plus the
machine-readable `usage` and `cost_comparison` objects:

```text
Cost & usage:
- Fusion cost: $0.0180 estimated
- Opus 5.5 baseline estimate: $0.0710 estimated
- Estimated savings: $0.0530 / 74.6%
- Fusion wall time: 8.4s
- Panel: 4 models, 3 succeeded, 1 failed
```

From the second run on, a lifetime footer is appended (aggregated by `fusion stats` and the
`fusion_stats` tool):

```text
- Lifetime: 42 runs · $0.85 spent vs $6.40 baseline est. (86.7% saved) · shadow win-rate 62% (n=8)
```

When a shadow A/B ran, the baseline cost and latency in `cost_comparison` are actual values rather
than estimates, and shadow cost is never counted as Fusion cost.

## Latency caveat

Fusion can accurately report its own wall time and model-call latencies. It does not invent
baseline latency. Baseline latency remains unknown unless a benchmark explicitly calls the
baseline model.

## Operational guidance

Run config validation after editing model, routing, pricing, or baseline YAML:

```bash
uv run fusion config validate
```

Use stored run comparisons for auditing:

```bash
uv run fusion runs show RUN_ID
uv run fusion runs compare-baseline RUN_ID
uv run fusion runs costs
```

When pricing changes, update the entry's price, `source_notes` and `updated_at`.
