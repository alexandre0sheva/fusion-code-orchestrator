# Costs and Baseline Comparison

Fusion tracks cost and usage at provider-call granularity. The goal is transparent
comparison, not false precision.

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
- `verified_on` and `source_url`: every cloud price must name where and when it was verified.

Local and mock models need no price block and are free. Token accounting rules:
`input_tokens` includes cached tokens, which are billed at the cached rate (or the input rate when
no cached rate is configured); reasoning tokens are assumed to be part of `output_tokens` unless a
separate `reasoning_per_1m` is set.

If provider billing returns an actual cost, Fusion prefers that. Otherwise it estimates from the
catalog price in effect today and the reported token usage. If pricing or token usage is missing,
cost is marked unknown rather than invented.

### Keeping prices honest

```bash
uv run fusion models check          # stale prices, price changes and retirements coming up
uv run fusion models check --live   # also ask each provider's free list-models endpoint
```

`check` warns when a price was verified more than 60 days ago, when the current price ends within
60 days, and when a provider's earliest retirement date for a model is near. `--live` sends your
API keys (in headers) to the providers' free `models` endpoints only; it makes no completion calls
and costs nothing.

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
benchmark mode. The default strategy estimates the cost of sending the same aggregate input/output
token volume to the baseline model. This is useful for directional comparison, but it is not the
same as actually running the baseline model.

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
