# Benchmarking

Canonical guide for measuring whether a Fusion panel of cheap models is cheaper, faster or better
than a single frontier model. Cost methodology is in [COSTS.md](COSTS.md); config in
[CONFIGURATION.md](CONFIGURATION.md).

## Methods available today

| Method | What it measures | Spends API money |
|--------|------------------|------------------|
| [Benchmark mode](#benchmark-mode-fusion-bench) | Cost, speed and ground-truth quality of several arms over a dataset, with every call recorded | Yes (live, capped) / No (`--mock`) |
| [Offline dataset evals](#offline-dataset-evals) | Score, latency and cost of Fusion on sample cases | Yes (live) / No (`--mock`) |
| [Claude Code A/B](#claude-code-ab) | Claude Code + Opus vs Claude Code + Fusion on your own tasks | Yes |
| [Shadow A/B](#shadow-ab-live-measurement) | Real baseline answer and blind pairwise verdict on live calls | Yes |

Benchmark mode is the framework the 0.2.0 study is built on. Its scorers, datasets, statistics and
report are added task by task in the
[0.2.0 roadmap](superpowers/plans/2026-10-05-v0.2.0-roadmap.md) (Tasks 16 to 19); this page says what
exists today.

## Benchmark mode (`fusion bench`)

A study answers one question: for tasks whose right answer is known, is a panel of cheap models
cheaper, faster or better than one frontier model? It runs **arms** over a **dataset**, measures
every call, scores each answer against the task's ground truth, and keeps everything on disk.

### Concepts

- **Task** (`BenchTask`): one line of a JSONL file (or one YAML document) with an `id`, a `category`
  (`code_review`, `debugging`, `architecture`, `planning`, `coding`, `frontend`, `performance`), a
  `prompt`, optional `context` and `files`, a `difficulty`, and `truth`: the category-specific
  ground truth. Prompts must be unique. A bare dataset name such as `toy` is looked up in
  `src/fusion/bench/datasets/`, then `evals/datasets/bench/`.
- **Arm** (`Arm`): a [strategy](CONFIGURATION.md#strategies-and-budgets) by name, with optional
  `overrides` (nested mappings merge), so "the cheap panel with three rounds" is one entry. The
  `default` arm set is the six of the roadmap: `solo-frontier`, `solo-cheap`, `panel-cheap`,
  `panel-refine`, `panel-cascade`, `panel-cheap-strong-synth`.
- **Job**: one (task, arm, repeat), keyed by a hash of the task, the arm's strategy and the model
  ids behind it, the repeat and the seed. A finished job is never run twice.
- **Mode**: every job runs in `Mode.BENCHMARK` ([what that changes](CONFIGURATION.md#modes)):
  temperature 0, a seed per repeat (`seed + repeat - 1`), no prompt trimming, no shadow run, every
  call streamed so time to first token and decode speed are measured, and no secret redaction
  (ground-truth tasks may contain secret-looking strings on purpose; `--redact` turns it back on).

### Commands

```bash
# 1. See what a study would cost and take. No model is called; needs no API keys.
uv run fusion bench plan --dataset toy --arms default --repeats 3 --max-usd 5

# 2. Run it. Free and deterministic with simulated models:
uv run fusion bench run --dataset toy --arms default --mock
#    ...or live, within a spending limit you state:
uv run fusion bench run --dataset my.jsonl --arms default --repeats 3 --max-usd 5

uv run fusion bench list                  # runs, newest first
uv run fusion bench show RUN              # one summary row per arm (--json for scripts)
uv run fusion bench resume RUN --max-usd 8   # continue a stopped or interrupted run
uv run fusion bench spend                 # the live-spend ledger
uv run fusion bench calibrate-judge --dataset toy --mock   # how well do judges pick the better answer?
uv run fusion bench dataset validate evals/datasets/v1 --release   # check a dataset (also: stats, build)
```

`--arms` takes strategy names (`solo-cheap,panel-cheap`), `name=strategy` to rename, or `default`.
`--config study.yaml` supplies any `BenchConfig` field, including arms with `overrides`; options on
the command line win. Other options: `--limit N` (use N tasks, spread over categories and chosen by
seed), `--seed`, `--concurrency` (jobs at once, default 8), `--no-cache`, `--redact`,
`--judge-models`, `--split dev|test|all` (see [Datasets](#datasets)).

### Planning and the cost caps

`bench plan` prices every (task, arm) with the arithmetic of the
[hard cost caps](CONFIGURATION.md#cost-and-latency-caps), from the catalog's prices and the task's own
size, plus what the scorer will spend. The result is a range, not a promise: answers are assumed
1,500 tokens long, a cascade is assumed to ask its whole panel in 40% of cases, and the low and
high ends scale every token count by 0.3 and 2.5. The time estimate follows the critical path of
each strategy using the catalog's latency tiers. When the expected cost is over the budget, the
plan suggests the largest study that fits, shrinking in a fixed order: fewer repeats, then fewer
tasks, then fewer arms (the dearest first). `bench run` refuses to start a live study whose plan
does not pass unless `--yes` is given.

Two caps hold a live study:

- **`--max-usd`** limits the run. Before a job starts, its worst case (a cascade escalating, every
  call at the assumed size) is reserved against what is left; when it will not fit, the run stops
  cleanly with status `stopped` and can be resumed with more money. Estimates are not
  measurements, so a run can pass the cap by at most the excess of the jobs in flight.
- **The live-spend ledger** limits everything ever run live. The roadmap allows **$20 in total**.
  Every live job appends its cost (arm and scorer) to `bench-results/spend.json`, a JSON array of
  `{date, task, purpose, usd}` that is only ever appended to, and no job starts that would take the
  total past the cap. A damaged ledger stops live runs rather than being reset. Simulated runs spend
  nothing and are never recorded. `fusion bench spend` shows it.

### What is measured

Each item stores the answer, the arm's claims, the full ledger of calls (`CallRecord`) and a
`BenchMetrics` block, all measured and none derived later from guesses:

| Field | Meaning |
|-------|---------|
| `seconds_to_complete` | wall time from request to final answer |
| `cost_usd` | every call the arm made; `eval_cost_usd` is the scorer's, kept apart |
| `input_tokens`, `output_tokens`, `reasoning_tokens` | summed over the arm's calls |
| `output_tokens_per_s` | effective: output tokens divided by wall seconds |
| `decode_tokens_per_s`, `ttft_ms` | per model, from streamed calls |
| `calls`, `retries` | provider calls and the retries inside them |
| `quality`, `solved` | the scorer's output, and whether it reached the category's pass threshold (0.6) |
| `cache_hits`, `latency_valid` | calls replayed from the cache; false when any were, see below |

Money per solved task is the arm's total cost divided by its solved items; `bench show` prints it.

A job ends `completed`, `halted` (the pipeline stopped early, for instance without quorum: the arm
failed the task, and the item is scored and counted) or `error` (an exception, or a scorer
failure). Errors keep what the run had spent and are tried again by `resume`.

### Resuming, results and the cache

Results go to `bench-results/<run>/results.jsonl` (one line per finished job, flushed as written;
this file is the record of truth), `config.json`, and the `bench_runs` and `bench_items` tables of
`bench-results/bench.db` (the same schema as the run history, in a separate file so studies never
appear in `fusion stats`). `bench-results/` is git-ignored; `FUSION_BENCH_DIR` moves it. After a
kill, `resume` skips the job keys already finished and runs the rest; a torn last line is ignored.
Repeats come first in the job order, so a run that stops early has whole repeats of every arm.

Live runs wrap each provider in a **response cache** (`bench-results/cache/`, one file per answer,
keyed by provider and the whole request including model, prompt, schema, temperature and seed).
A replayed call is billed at zero, flagged `cache_hit` on its record, and keeps the latency and
speed it had when first answered. Only successes are stored. Because repeats use different seeds,
they never share entries; a rerun of the same repeat does. A job with any cache hit has
`latency_valid: false`: its wall time is not a measurement of the arm, so summaries leave it out of
the percentiles. Use `--no-cache` for a speed study. The cache is off for `--mock`.

### Simulated models (`--mock`)

`--mock` runs the real pipeline, with the real strategies, catalog models and prices, on
`SimulatedProvider`s (`providers/simulated.py`). Each simulated model has a skill (from the
catalog's quality tier), a price, a speed (from its latency tier) and an error rate. It knows each
task's ground truth and finds each true point with a probability set by its skill and the task's
difficulty, sometimes asserting a known-wrong decoy. Whether it finds a point is the sum of three
things: how hard that point is for every model, how hard it is for models of the same provider, and
its own luck, so models make **correlated mistakes**, which is what decides whether a panel can
beat one model. Every draw is a hash of the seed, the model, the task, the point and the request's
seed: reruns are identical, and a different repeat has different luck.

Simulated runs also use a **virtual clock** (`bench/virtual.py`): when every task is asleep the
event loop jumps to the next wake-up, so a study of simulated twelve-second calls takes a second of
real time, parallel calls still overlap, and timings come out identical on every run (to the last
few bits: a job that starts later on the clock subtracts larger numbers). The simulation validates
the harness, the statistics and the cost caps offline; **its skill numbers are assumptions, not
measurements, and say nothing about the real models**.

### Scoring

Ground truth first, an LLM judge second. A scorer (`bench/scoring/`) turns the arm's *final answer*
into a quality in [0, 1]; the claims behind it are never read, so an aggregator that dropped a point
gets no credit for a model having raised it. One scorer is registered per category, reads the
`truth` format below, and falls back to `PointsScorer` for a task whose truth is in `points`
format. A judge is called only for what the deterministic checks cannot decide, and only when the
study names `--judge-models` (catalog aliases); without them every scorer is free and offline.
Judge spend is `eval_cost_usd`, kept apart from the arm's cost and included in `plan` and in
`--max-usd`. Every scorer also accepts optional `Evidence` (measured test, timing or visual
results) so that later evaluators can feed the rubric without a new interface.

| Category | Scorer | `truth` | Quality |
|---|---|---|---|
| `code_review` | `ReviewScorer` | `bugs`: `[{file, line, category, severity, description, aliases?}]`, `line_tolerance` (3); empty `bugs` = a clean change | F1 of severity-weighted recall and precision |
| `debugging` | `DebugScorer` | `root_cause_tags`, `root_cause_aliases?`, `root_cause?`, `fix_keywords?` | 0.7 × cause credit + 0.3 × share of fix keywords |
| `architecture`, `planning` | `RubricScorer` | `required_points`, `forbidden_points`: strings or `{id, text, keywords?, gate?, weight?}` | weighted share of required items met, less 0.5 × share of forbidden asserted |
| any | `PointsScorer` | `points`, `decoys`: `{id, keywords, text?, weight?}` | weighted recall less 0.5 × share of decoys asserted |

`coding`, `frontend` and `performance` are scored on measured results by the evaluators of roadmap
Tasks 16 and 17; until then only a `points` truth scores them.

**Review.** The answer's *findings* are the places it points at (`file:line`, `file, line N`, or a
bare `line N` when the task has one file). A finding reports a seeded bug when it names the bug's
file within `line_tolerance` lines: full credit if it also says the bug's category (or an alias),
half if it only points there. Unmatched findings are false positives; one in a file the task does
not contain is a *hallucinated file* and counts double. Only bugs still unmatched go to the judge
(the first of `--judge-models`) together with the leftover findings; a finding with no location is
never penalised, since without a location it cannot be called false. A clean change scores
`1 / (1 + false-positive weight)`: one false alarm leaves 0.5, below the pass mark.

**Debugging.** `root_cause_tags` are equivalent names for the one root cause. The answer's
hypotheses are its list entries (paragraphs if it has no list), in order; the rank of the first one
naming the cause gives 1.0 (first), 0.7 (second or third), 0.3 (later), 0 (none). If none names it
in so many words the judge is asked whether one of the first five says the same thing.

**Rubric.** Each item is judged met or not and the judge must *quote* the answer; a quote that is in
neither the answer nor the evidence voids the verdict. With several judges each item goes by
majority. Items with `keywords` need no judge. An item with `gate: true` that is missed caps quality
at 0.4. If no item can be decided (no judge, no keywords) the task errors rather than guesses.

**Pairwise judge** (`PairwiseJudge`, used for A/B comparisons between arms). Every judge sees both
orderings; a judge that names a different answer in each (it followed the position) is a tie, and
the rate is reported as position bias. Equal judge votes tie. **Cross-family rule:** a judge may not
come from a provider that serves either arm (models favour their own family); with none left the
comparison refuses. Cost is the scorer's, never the arms'.

**Calibrating judges.** Before trusting a judge, measure it:

```bash
uv run fusion bench calibrate-judge --dataset my.jsonl --judge-models claude-haiku,gpt-luna --max-usd 1
uv run fusion bench calibrate-judge --dataset toy --mock      # simulated judges, free
```

Each task with a known truth yields a *good* answer (says what the truth says) and a *flawed* one
(says little of it and asserts what is wrong); `--cases file.jsonl` supplies `{task_id, good,
flawed}` pairs instead. Every judge compares every pair in both orderings and the report gives
accuracy, tie rate, position-flip rate, Cohen's κ against the truth (over both orderings, so an
always-"A" judge scores κ ≈ 0), agreement between judges and κ per pair. Reports are kept in
`bench-results/calibration/`; a live calibration is forecast first, refuses to exceed `--max-usd`
or the live-spend cap, and records its spend.

**Caveats.** The seeded pairs are *easy* by construction (a judge that fails them is unusable; one
that passes may still fail on subtle differences), and an LLM judge validated on them can still
favour verbose or confident answers. Keyword and word-overlap checks reward wording the truth
anticipated: write `aliases` and `keywords` generously, and prefer judged items for open-ended
points. Simulated judges (`--mock`) follow assumed skill and position-bias numbers; their reports
test the harness, not any real model.

### Datasets

`evals/datasets/v1/` is the ground-truth dataset the 0.2.0 study runs on: 100 tasks, 25 each of
`code_review`, `debugging`, `architecture` and `planning`, in three difficulties, with code tasks in
Python, TypeScript and Go, and six clean reviews (24%) that test false alarms. Truth formats and
scorers are under [Scoring](#scoring). **All tasks are synthetic and not yet reviewed by a person;
what that means for validity is in [evals/datasets/README.md](../evals/datasets/README.md#provenance-and-validity-read-this-before-quoting-a-number),
which is also where the licence note and the guidelines for adding tasks live.**

**Splits.** Every task is in `dev` (15 per category) or `test` (10 per category). `dev` is for tuning
(the cascade threshold, prompts, judge choice); `test` is touched only by the final study. A run uses
`--split dev` unless told otherwise (`BenchConfig.split`, default `dev`), so a tuning run cannot
see held-out tasks by accident; `--split test` and `--split all` are for the final study. A dataset
with no split labels (the toy dataset, your own file) is used whole under any split. The validator
rejects a prompt or a file set shared between the splits.

```bash
uv run fusion bench dataset validate evals/datasets/v1 --release   # schema, ids, lines, secrets, splits, coverage
uv run fusion bench dataset stats evals/datasets/v1                # counts per category, difficulty, split, language
uv run fusion bench dataset build                                  # compile authoring/v1/*.yaml into v1/*.jsonl
uv run fusion bench dataset build --check                          # fail if the JSONL is out of date
uv run fusion bench dataset build --generate 10 --category debugging --model claude-sonnet --max-usd 1
```

`validate` always checks each row's schema, unique ids and prompts, that bug files and line numbers
are inside the files supplied (and on added lines of the diff), that no string looks like a secret,
that a licence note sits next to the dataset, and that an answer written from a task's own truth
scores at least 0.9 under its offline scorer and a flawed one at most 0.5. `--release` adds the
published dataset's promise: at least 100 tasks, 25 per category, 8 per split and 3 per difficulty in
each category, three languages, 20% clean reviews, and 30 to 400 lines per review diff or debugging
task. Tasks are written as YAML (code review diffs mark each seeded bug with `«id»`, and the compiler
computes the line numbers), compiled to JSONL, and a test fails when the two differ. `--generate`
has a model draft candidates into `evals/datasets/authoring/candidates/` for a person to review; it
is forecast, held to `--max-usd` and the live-spend cap, and recorded in the spend ledger.

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
