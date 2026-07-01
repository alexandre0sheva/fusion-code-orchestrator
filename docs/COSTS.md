# Costs and Baseline Comparison

Fusion tracks cost and usage at provider-call granularity. The goal is transparent
comparison, not false precision.

## Pricing registry

Model prices are configured in `src/fusion/config/pricing.yaml` as USD per one million
tokens:

```yaml
pricing:
  anthropic.claude-opus-4-8:
    provider: anthropic
    model_id: claude-opus-4-8
    alias: anthropic.claude-opus-4-8
    input_price_per_1m_tokens: 15.0
    output_price_per_1m_tokens: 75.0
    currency: USD
    source_notes: Configured list-price placeholder; verify against current provider pricing.
    updated_at: "2026-07-01"
    is_estimate: true
```

Optional fields:

- `cached_input_price_per_1m_tokens`
- `reasoning_price_per_1m_tokens`

If provider billing returns an actual cost, Fusion prefers that. Otherwise it estimates from
configured pricing and token usage. If pricing or token usage is missing, cost is marked
unknown.

## Baseline model

The baseline is configured in `src/fusion/config/baseline.yaml`:

```yaml
baseline:
  name: "Opus 4.8"
  provider: "anthropic"
  model_id: "claude-opus-4-8"
  pricing_alias: "anthropic.claude-opus-4-8"
  enabled: true
  estimate_strategy: "same_input_and_output_tokens"
```

The default strategy estimates the cost of sending the same aggregate input/output token
volume to the baseline single model. This is useful for directional comparison, but it is
not the same as actually running the baseline model.

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

Keep `is_estimate: true` unless the price is verified from provider billing or current
published pricing. When exact pricing changes, update `source_notes` and `updated_at`.
