# Benchmarking

Canonical guide for measuring whether a Fusion panel of cheap models is cheaper, faster or better
than a single frontier model. Cost methodology is in [COSTS.md](COSTS.md); config in
[CONFIGURATION.md](CONFIGURATION.md).

## Methods available today

| Method | What it measures | Spends API money |
|--------|------------------|------------------|
| [Offline dataset evals](#offline-dataset-evals) | Score, latency and cost of Fusion on sample cases | Yes (live) / No (`--mock`) |
| [Claude Code A/B](#claude-code-ab) | Claude Code + Opus vs Claude Code + Fusion on your own tasks | Yes |
| [Shadow A/B](#shadow-ab-live-measurement) | Real baseline answer and blind pairwise verdict on live calls | Yes |

A ground-truth benchmark mode (`fusion bench`) with statistics and reports is being built in the
[0.2.0 roadmap](superpowers/plans/2026-10-05-v0.2.0-roadmap.md) (Tasks 13 to 19). This page is extended
as it lands.

## Offline dataset evals

Datasets live in `evals/datasets/` as JSONL: `code_review`, `debugging`, `architecture`, `planning`.

```bash
# Live eval on code review cases (costs API credits)
uv run python evals/runners/run_offline_eval.py --dataset code_review

# Save results for analysis
uv run python evals/runners/run_offline_eval.py \
  --dataset debugging \
  --output evals/results/debugging-$(date +%Y%m%d).jsonl

# Compare all task types quickly, or run without keys
uv run python evals/runners/compare_pipelines.py
uv run python evals/runners/run_offline_eval.py --dataset code_review --mock
```

Add custom cases to `evals/datasets/*.jsonl`:

```json
{"id": "my-case-1", "diff": "...", "context": "...", "expected_themes": ["auth", "sql"]}
```

## Evaluating Fusion on your own task

1. Prepare the input: a diff, an error with logs, an architecture question or a feature description.
2. Run it through the CLI and note the `run_id`:

   ```bash
   uv run fusion review-diff --file my-change.patch > result.json
   uv run fusion runs show RUN_ID
   ```

3. Inspect the scores in the output:
   - `evals.final.overall_score`: hybrid quality score (0 to 1)
   - `evals.deterministic`: safety and completeness flags
   - `disagreement.disagreement_score`: panel disagreement
   - `routing.selected_panel`: which models ran
   - `confidence`: synthesis confidence

## Claude Code A/B

Compare "Claude Code + Opus" against "Claude Code + Fusion MCP + cheaper models" on the same tasks.
Claude Code stays the executor in both arms; Fusion only replaces the expensive reasoning step.

1. Pick 5 to 10 real tasks from your repo (diffs, bugs, architecture decisions).
2. Run each task in Claude Code with the Opus/native model; save the answer and note time, cost and
   iterations.
3. Run the same task in Claude Code calling `fusion_ask` or the matching specialized tool; let Claude
   Code apply the answer and run tests.
4. Compare with `fusion_compare_claude_runs`, or from saved files:

   ```bash
   uv run fusion compare-claude-runs \
     --task-file task.md \
     --opus-file claude-opus-output.md \
     --fusion-file claude-fusion-output.md \
     --context-file verification.md \
     --opus-cost 0.42 --fusion-cost 0.07 \
     --opus-latency-ms 90000 --fusion-latency-ms 45000
   ```

The exact prompts, folder layout and checklist are in [CLAUDE_CODE_AB.md](CLAUDE_CODE_AB.md).

Dimensions to compare:

| Dimension | What to measure |
|-----------|-----------------|
| Correctness | Did it catch the bug / pick the right architecture? |
| Groundedness | Unsupported claims (`evals.deterministic`, `unsupported_claims`) |
| Specificity | Actionable steps vs vague advice (`evals.llm_judge.specificity`) |
| Safety | Secret leakage, dangerous commands flagged |
| Disagreement value | Did the panel surface issues a single model missed? |
| Cost and latency | `total_cost_usd`, `total_latency_ms` in the run record |
| Iterations | Back-and-forth turns needed to reach an acceptable result |

Where Fusion is expected to help: security-sensitive review, high-stakes architecture decisions,
debugging with ambiguous symptoms, and answers you want scored before merging. A single frontier
model is expected to win on small localized edits, thin context, and latency-sensitive loops where
panel fan-out is too slow. These are hypotheses to test, not results.

## Shadow A/B (live measurement)

To measure quality against the real baseline instead of an estimate, enable shadow mode. Fusion then
also sends the same sanitized task to the baseline model; a blind pairwise judge scores both answers
in randomized order, and the verdict plus actual baseline cost and latency are stored in SQLite.

```bash
# .env: off (default) | sampled | always
FUSION_SHADOW_MODE=sampled
FUSION_SHADOW_SAMPLE_RATE=0.2
```

Per-call override on any orchestration tool: `shadow_baseline: true` (or `false`). Shadow calls cost
real API money and are never counted as Fusion cost. Any shadow failure degrades to a warning; the
main run always succeeds. Internals: [ARCHITECTURE.md](ARCHITECTURE.md#shadow-baseline-ab).

View cumulative results:

```bash
uv run fusion stats            # spend, savings, shadow win-rate
uv run fusion stats --json     # machine-readable
```

Inside Claude Code, call `fusion_stats`. Every run's `display_markdown` also ends with a lifetime
footer (see [COSTS.md](COSTS.md#what-claude-code-sees)).

## History

An earlier isolated agent harness (an Opus agent versus a Fusion-planned agent in workspace copies) was removed in 0.2.0. Its only real
run, in June 2026, was inconclusive because both arms hit the step cap without writing files; the
record is kept in [evals/archive/2026-06-30-agent-harness-benchmark.md](../evals/archive/2026-06-30-agent-harness-benchmark.md).
