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
report were built in 0.2.0 and are extended in the
[0.3.0 roadmap](superpowers/plans/2026-11-17-v0.3.0-roadmap.md) (Tasks 10 and 13 to 16); this page says
what exists today.

## Benchmark mode (`fusion bench`)

A study answers one question: for tasks whose right answer is known, is a panel of cheap models
cheaper, faster or better than one frontier model? It runs **arms** over a **dataset**, measures
every call, scores each answer against the task's ground truth, and keeps everything on disk.

### Concepts

- **Task** (`BenchTask`): one line of a JSONL file (or one YAML document) with an `id`, a `category`
  (`code_review`, `debugging`, `architecture`, `planning`, `coding`, `frontend`, `performance`), a
  `prompt`, optional `context` and `files`, a `difficulty`, and `truth`: the category-specific
  ground truth. Prompts must be unique. A bare dataset name such as `toy` is looked up in
  `src/fusion/bench/datasets/`, then `evals/datasets/bench/`, `evals/bench/`, `evals/datasets/v1/`
  (so `coding` is `v1/coding`) and `evals/datasets/` (so `v1` is the whole shipped dataset).
- **Arm** (`Arm`): a [strategy](CONFIGURATION.md#strategies-and-budgets) by name, with optional
  `overrides` (nested mappings merge), so "the cheap panel with three rounds" is one entry. The
  `default` arm set is six: `solo-opus`, `solo-haiku`, `panel-nano`, `panel-refine`,
  `panel-cascade`, `panel-cheap-strong-synth`.
- **Job**: one (task, arm, repeat), keyed by a hash of the task, the arm's strategy and the model
  ids behind it, the repeat and the seed. A finished job is never run twice.
- **Mode**: every job runs in `Mode.BENCHMARK` ([what that changes](CONFIGURATION.md#modes)):
  temperature 0 and a seed per repeat (`seed + repeat - 1`) for models whose catalog entry accepts
  sampling parameters (none of the packaged ones do since 0.3.0), no prompt trimming, no shadow run, every
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
uv run fusion bench show RUN --task ID    # one task: gates, criteria, evidence, the judge's reasoning
uv run fusion bench resume RUN --max-usd 8   # continue a stopped or interrupted run
uv run fusion bench resume RUN --retry-halted   # ...and run again the jobs that halted (timeouts, no quorum)
uv run fusion bench report RUN            # intervals, paired comparisons, $ per solved task, verdicts (md)
uv run fusion bench report RUN --format html   # one self-contained file with the charts
uv run fusion bench compare RUN_A RUN_B   # before and after a tuning change
uv run fusion bench spend                 # each budget's live spend and what is left
uv run fusion bench suite list            # the packaged studies; `suite show NAME` for one
uv run fusion bench plan --suite ablation # price a suite (also: run --suite NAME)
uv run fusion bench recommend RUN         # which arm to make the default, from a dev run
uv run fusion bench results RUN           # write docs/BENCHMARK_RESULTS.md from runs
uv run fusion bench calibrate-judge --dataset toy --mock   # how well do judges pick the better answer?
uv run fusion bench calibrate-judge --dataset v1 --mock --artifacts   # ...the better page or faster code?
uv run fusion bench dataset validate evals/datasets/v1 --release   # check a dataset (also: stats, build)
```

`--arms` takes strategy names (`solo-haiku,panel-nano`), `name=strategy` to rename, or `default`.
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
  call at the assumed size, plus a 25% margin because real answers run longer than the assumed
  size) is reserved against what is left; when it will not fit, the run stops
  cleanly with status `stopped` and can be resumed with more money. Estimates are not
  measurements, so a run can pass the cap by at most the excess of the jobs in flight.
- **The live-spend ledger** limits everything run live under one roadmap's budget: **$15 for v0.3.0**
  (the current budget; v0.2.0 had $20). Every live job appends its cost (arm and scorer) to
  `bench-results/spend.json`, a JSON array of `{date, task, purpose, usd, budget}` that is only ever
  appended to, and no job starts that would take its budget's total past the cap. An entry without
  `budget` was written by 0.2.0 and counts as `v0.2.0`, never against `v0.3.0`. A damaged ledger
  stops live runs rather than being reset. Simulated runs spend nothing and are never recorded.
  `fusion bench spend` shows each budget's total and what is left.

### What is measured

Each item stores the answer, the arm's claims, the full ledger of calls (`CallRecord`) and a
`BenchMetrics` block, all measured and none derived later from guesses:

| Field | Meaning |
|-------|---------|
| `seconds_to_complete` | wall time from request to final answer |
| `cost_usd` | every call the arm made; `eval_cost_usd` is the scorer's, kept apart |
| `eval_cost_usd`, `eval_seconds` | what measuring and judging the answer cost in money and wall time (frontend and performance tasks: tests, timings, a browser, a judge); never added to the arm's cost or `seconds_to_complete`, shown beside them |
| `input_tokens`, `output_tokens`, `reasoning_tokens` | summed over the arm's calls |
| `output_tokens_per_s` | effective: output tokens divided by wall seconds |
| `decode_tokens_per_s`, `ttft_ms` | per model, from streamed calls |
| `calls`, `retries` | provider calls and the retries inside them |
| `quality`, `solved` | the scorer's output, and whether it reached the category's pass threshold (0.6; for `coding`, 1.0: every hidden test passes) |
| `cache_hits`, `latency_valid` | calls replayed from the cache; false when any were, see below |

Money per solved task is the arm's total cost divided by its solved items; `bench show` prints it.

A job that halted because a call hit the fan-out's 45-second per-model timeout scores 0, which
understates a slow model (GPT-6.1 Sol timed out on 21 of its 68 final-study jobs, Opus on 3).
`resume --retry-halted`, with the limits raised for that run (`FUSION__FANOUT__PER_MODEL_TIMEOUT_SECONDS=240
FUSION__FANOUT__GLOBAL_TIMEOUT_SECONDS=300`), runs those jobs again; every attempt stays in
`results.jsonl`, the last is scored, and the report says how many jobs were retried.

A job ends `completed`, `halted` (the pipeline stopped early, for instance without quorum: the arm
failed the task, and the item is scored and counted) or `error` (an exception, or a scorer
failure). Errors keep what the run had spent and are tried again by `resume`.

### Resuming, results and the cache

Results go to `bench-results/<run>/results.jsonl` (one line per finished job, flushed as written;
this file is the record of truth), `config.json`, `meta.json` (what the run was when it started: the
dataset hash, each arm's models and each model's price and the date it was verified; a resumed run
keeps it), `report.html` once you ask for one, and the `bench_runs` and `bench_items` tables of
`bench-results/bench.db` (the same schema as the run history, in a separate file so studies never
appear in `fusion stats`). `bench-results/` is git-ignored; `FUSION_BENCH_DIR` moves it. After a
kill, `resume` skips the job keys already finished and runs the rest; a torn last line is ignored.
Repeats come first in the job order, so a run that stops early has whole repeats of every arm.

Live runs wrap each provider in a **response cache** (`bench-results/cache/`, one file per answer,
keyed by provider and the whole request including model, prompt, schema, temperature and seed).
A replayed call is billed at zero (the provider did not charge for it), flagged `cache_hit` on its
record, and keeps the latency and speed it had when first answered. **Reports still count it at
its price-list cost** (`list_cost_usd` on the record, `replayed_cost_usd` on the item): two arms
that make the same call, such as a panel's Haiku request and a solo Haiku arm's, would otherwise
look cheaper the second time. The spend ledger counts only what was billed. Only successes are stored. Because repeats use different seeds,
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
results) so that the evaluators' output can feed a rubric without a new interface
(`to_score_evidence` converts it).

| Category | Scorer | `truth` | Quality |
|---|---|---|---|
| `code_review` | `ReviewScorer` | `bugs`: `[{file, line, category, severity, description, aliases?}]`, `line_tolerance` (3); empty `bugs` = a clean change | F1 of severity-weighted recall and precision |
| `debugging` | `DebugScorer` | `root_cause_tags`, `root_cause_aliases?`, `root_cause?`, `fix_keywords?` | 0.7 × cause credit + 0.3 × share of fix keywords |
| `architecture`, `planning` | `RubricScorer` | `required_points`, `forbidden_points`: strings or `{id, text, keywords?, gate?, weight?}` | weighted share of required items met, less 0.5 × share of forbidden asserted |
| `coding` | `CodingScorer` | `expected_pass`, `hidden_files`, `command`, `timeout_s`, `solution?`, `flaws?` (see [Coding tasks](#coding-tasks-and-the-sandbox)) | pass@1: the share of `expected_pass` hidden tests that pass |
| `frontend`, `performance` | `ArtifactScorer` | the coding keys plus `hard_gates`, `soft_criteria`, `evaluators?`, `perf?`, `site?` (see [Frontend and performance tasks](#frontend-and-performance-tasks-evidence-and-the-agentic-judge)) | completion score: 0 if a hard gate fails, else the weighted mean of the soft criteria |
| any | `PointsScorer` | `points`, `decoys`: `{id, keywords, text?, weight?}` | weighted recall less 0.5 × share of decoys asserted |

A `frontend` or `performance` task in `points` format still scores on its points.

**Coding.** The answer's one patch is run against hidden tests in a sandbox; no model is asked and
the scorer costs nothing. How: [Coding tasks and the sandbox](#coding-tasks-and-the-sandbox).

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
come from a vendor whose models serve either arm (models favour their own family); with none left
the comparison refuses. The vendor is the model's maker, whichever API serves it
(`ModelEntry.vendor`: Claude through OpenRouter is Anthropic, `qwen/qwen3.8-flash` is Qwen), so an
open-weight panel excludes only its own makers' judges. Cost is the scorer's, never the arms'.

**Neutral judges.** `neutral_judges: true` in a study (the `*-v3` suites set it) refuses to plan or
run it unless every judge's vendor is on no arm: not among the members and aggregators of any arm
(including the catalog synthesizer the router picks for an aggregator the strategy does not name,
and an in-run judge), on every rung `strategy: auto` may climb (`fusion.bench.judges`). Then every task of every arm is
judged by the same impartial judges, instead of the cross-family rule dropping a judge per arm. A
same-tier spare that stands in for a failed member is not known in advance and is not checked.

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

**The v2 calibration (2026-11-18, live, $0.197 of Task 14's $1).** Candidates were OpenRouter models
from vendors on no 0.3.0 panel. Text judges, on the 60 seeded pairs of the v2 dev split, both
orderings: MiMo-V2.6 Flash 97% accuracy (3 failed calls of 120), Muse Ember 93% (6), MiMo-V2.6 Pro 93%
(7), Hy4 Flash 75% (24 failed calls, so ties), no order flips. The agentic judge (`--artifacts
--cases-per-set 4`, 8 frontend and performance cases): MiMo Pro 100%, Muse Ember 88%, MiMo Flash 62%
(below the 80% floor, so a study that names it gets no headline verdict). The `*-v3` suites
therefore use **MiMo Pro and Muse Ember** (Xiaomi and Meta, on no arm). With two judges a rubric item
split 1-1 counts as not met. The report does not keep why a call failed (an error or no JSON).

**Caveats.** The seeded pairs are *easy* by construction (a judge that fails them is unusable; one
that passes may still fail on subtle differences), and an LLM judge validated on them can still
favour verbose or confident answers. Keyword and word-overlap checks reward wording the truth
anticipated: write `aliases` and `keywords` generously, and prefer judged items for open-ended
points. Simulated judges (`--mock`) follow assumed skill and position-bias numbers; their reports
test the harness, not any real model.

### Datasets

**`evals/datasets/v2/`** is the dataset of the 0.3.0 studies (the `*-v3` suites): every `v1` task
with its id and split, three more frontend and three more performance tasks, and 34 **real-repo**
coding tasks, upstream bugs of small open-source projects scored by the maintainers' own tests
([Dataset D](../evals/datasets/README.md#dataset-d-real-repo-coding-tasks-v2)): 197 tasks, at least
15 per category. Its split was **frozen before any live run** (`_splits.json`, written by `fusion
bench dataset freeze`; validation fails if a task moves or is not in it, and `freeze --add` takes in
a new one), its release promise is data (`_dataset.yaml`, read by `validate --release`), and
`_review.yaml` is the owner's review checklist (`fusion bench dataset review`), described in
[evals/datasets/README.md](../evals/datasets/README.md#the-owners-review). Files whose names start
with `_` are never read as tasks.

```bash
uv run fusion bench dataset validate evals/datasets/v2 --release   # also the frozen split and the review sheet
uv run fusion bench dataset freeze evals/datasets/v2 [--add]       # freeze the split (once), or take in new tasks
uv run fusion bench dataset review evals/datasets/v2               # create or refresh the review sheet
```

A real-repo task runs `pytest` over `tests/` in the sandbox (the parent commit's test file is the
visible test, the fix commit's is the hidden one), and is solved when every hidden test that passes
with the upstream fix passes (fail-to-pass and pass-to-pass). The rest of this section describes
`v1`, whose tasks `v2` contains unchanged.

`evals/datasets/v1/` is the ground-truth dataset the 0.2.0 study runs on: 157 tasks, 25 each of
`code_review`, `debugging`, `architecture` and `planning` (in three difficulties, with code tasks in
Python, TypeScript and Go, and six clean reviews, 24%, that test false alarms), 33 executable
`coding` tasks that are scored by running tests ([below](#coding-tasks-and-the-sandbox)), and 12
`frontend` and 12 `performance` tasks that are scored on what the answer builds
([after that](#frontend-and-performance-tasks-evidence-and-the-agentic-judge)). Truth formats and
scorers are under [Scoring](#scoring). **All tasks are synthetic and not yet reviewed by a person;
what that means for validity is in [evals/datasets/README.md](../evals/datasets/README.md#provenance-and-validity-read-this-before-quoting-a-number),
which is also where the licence note and the guidelines for adding tasks live.**

**Splits.** Every task is in `dev` (15 per text category, 16 coding tasks, 6 each of frontend and
performance) or `test` (10 per text category, 17 coding tasks, 6 each of frontend and performance). `dev` is for tuning
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
that a licence note sits next to the dataset (and a `LICENSE` beside each real-repo task), that no
prompt repeats a line of 32 or more characters from the hidden tests or the reference solution,
and that an answer written from a task's own truth
scores at least 0.9 under its offline scorer and a flawed one at most 0.5, and it **runs every
executable task** (see below; about 15 seconds for the 33 coding tasks, a minute for the 12
performance tasks, whose timings are taken one at a time). `--release` adds the published dataset's
promise: at least 100 tasks, 25 per text category, at least 30 coding tasks and 12 each of frontend
and performance, 8 per split (4 for frontend and performance) and 3 per difficulty in each
category, three languages, 20% clean reviews, and 30 to 400 lines per review diff or debugging task;
a dataset's `_dataset.yaml` replaces these numbers with its own (v2: 120 tasks, 15 per category, 30
real-repo tasks). Tasks are written as YAML (code review diffs mark each seeded bug with `«id»`, and the compiler
computes the line numbers), compiled to JSONL, and a test fails when the two differ. `--generate`
has a model draft candidates into `evals/datasets/authoring/candidates/` for a person to review; it
is forecast, held to `--max-usd` and the live-spend cap, and recorded in the spend ledger.

### Coding tasks and the sandbox

The `coding` category is scored by running code, not by matching words. A task is a directory,
`evals/datasets/v1/coding/<task_id>/`:

| Path | Holds |
|------|-------|
| `prompt.md` | what to build or fix; the format request for the answer is appended when the task loads |
| `repo/` | the files the model sees: the code, and a few **visible** tests (`repo/tests/`) |
| `tests/` | the **hidden** tests: copied in after the patch, never shown to a model |
| `reference/solution/`, `reference/flaw-N/` | the files of a correct fix and of plausible wrong fixes, as new content (the loader turns them into diffs) |
| `task.yaml` | `id`, `difficulty`, `split`, `tags`, `solution_summary`, `flaws` (one summary each) and `expected_pass` |

The loaded task's `truth` is `CodingTruth`: the test `command` (default
`python -m unittest discover -v -s tests -t .`), `timeout_s`, `mem_mb`, `hidden_files`,
`expected_pass` (the ids of the hidden tests a correct patch passes), and the reference `solution`
and `flaws` as diffs. The last two are for the validator and for simulated models; a real model
never sees any of `truth`. How to add a task is in
[evals/datasets/README.md](../evals/datasets/README.md#dataset-b-executable-coding-tasks).

**The answer is one patch.** No agent loop, so solo and panel arms are compared fairly: each answer
holds a single patch, either a unified diff or the complete new content of each changed file under
`=== path ===` lines. Panelists put it in the `patch` field of their claims JSON
(`PanelAnswer.patch`); the `llm` aggregator is asked for one patch merged from theirs; `vote` and a
cascade's early exit return the patch most models gave (identical up to whitespace), as that model
wrote it; `best_of` returns the answer the others' claims back most. `panel-digest` returns every
answer for Claude Code to merge, so it holds every panelist's patch and scores 0 whenever they
differ: do not benchmark it on coding. Diffs are applied by matching context, not by trusting hunk numbers,
because models get line numbers wrong; a patch that does not apply, names an unsafe path, or is
missing scores 0, and the item's `score.details` says which.

**Scoring.** The scorer applies the patch to the task's files in a fresh sandbox, copies the hidden
files over the result (so a patch cannot weaken a test), and runs the command. Quality is **pass@1**:
the share of `expected_pass` that pass, and a task is *solved* only at 1.0. **Flake guard:** when the
first run does not pass everything, it is run twice more (fresh sandboxes) and a test counts as
passed if it passed in most runs; tests whose result changed are listed as `flaky`. A run that times
out is not repeated. Results are memoised by (task, patch), so identical patches from several arms
or repeats run once.

**The sandbox** (`bench/sandbox.py`, a reusable `Sandbox` context manager with `copy_in`, `run` and
`collect`, which the [evaluators](#frontend-and-performance-tasks-evidence-and-the-agentic-judge) also use) runs each command in a temporary directory
with a wall-clock limit that kills the whole process group, `resource` limits (CPU, file size, core
dumps; memory on Linux), output caps, a scrubbed environment (only `PATH` and locale pass: no API
keys, `HOME` and `TMPDIR` inside the sandbox) and, where the platform offers it, **no network and no
writes outside the sandbox** (`sandbox-exec` on macOS, `unshare --net` on Linux). Where it does
not, `score.details["isolation"]` says `none` and only the limits apply. `FUSION_SANDBOX_ISOLATION`
chooses: `auto` (default), `require` (refuse to run without isolation), `off`. It is a best-effort
guard for a dataset you chose, not a security boundary; see [SECURITY.md](../SECURITY.md#benchmark-sandbox).
It runs only inside benchmark mode, on tasks from the dataset you named, and is never reachable
through MCP.

**`best-of-n-verified`** (a strategy, not in the `default` six): each of the three cheap models
writes a patch and the task's *visible* tests choose, with no synthesis call: the patch with the
highest visible pass rate wins (a patch that does not apply loses; ties go to the patch more models
gave, then to claim agreement). The visible tests are weaker than the hidden ones on purpose: the
arm measures what cheap verification buys, and on the shipped tasks they catch about two thirds of
the wrong fixes. It runs code, so the pipeline **refuses it outside benchmark mode**
(`BenchmarkOnlyError`): Fusion's tools never execute anything and Claude Code stays the only
executor. Verification time counts toward the arm's wall time in live studies; with `--mock` it
takes no virtual time, so simulated timings leave it out.

**`verified-synth`** (a strategy, not in the `default` six) generalises it: the visible tests are
no longer needed. Each of the three cheap models writes a complete patch (from a `patch` approach:
minimal fix, root-cause fix) **and** one to three check scripts that should fail on the code as it
is and pass once it is fixed, plus guardrails. The checks are validated, run on the unpatched copy
(a check that already passes is dropped: it cannot discriminate), then every patch is run against
every check in its own sandbox copy; the task's visible tests, when it has any, join as a trusted
check. A patch scores 1 per check another model (or the task) wrote that it passes and 0.5 per check
its own author wrote, so two models that agree on a wrong fix cannot outvote one that passes
everyone's checks; a tie goes to one `select` call among the tied patches. The matrix, every check's
fate and the reason for the pick are kept in each item's `verification` field of `results.jsonl`
(how it works: [ARCHITECTURE.md](ARCHITECTURE.md#verified-generation-checks-and-the-cross-check)).
Like `best-of-n-verified` it runs code, so it is refused outside benchmark mode. Hidden tests are
never used to pick: they grade the result afterwards, and a test checks that no panel prompt
contains them. Simulated models write checks that compare file hashes against their own patch and,
when the model "understood" the task (a draw on its skill), against the reference fix: they are
oracles, so a `--mock` run of this arm is an upper bound on what real checks achieve.

```bash
uv run fusion bench dataset validate evals/datasets/v1/coding         # runs every task's solution, flaws and tests
uv run fusion bench plan --dataset coding --arms solo-opus,panel-nano,best-of-n-verified --mock
uv run fusion bench run  --dataset coding --arms solo-opus,panel-nano,best-of-n-verified,verified-synth --mock
```

**Caveats.** The tasks are small, self-contained Python and standard library only (the sandbox runs
any command, but no other language is shipped), written by an LLM and not yet reviewed by a person
([provenance](../evals/datasets/README.md#provenance-and-validity-read-this-before-quoting-a-number)).
Hidden tests check what the prompt specifies, so a correct patch that reads the prompt differently
fails; the tests were run against a reference fix and two wrong ones, which proves they can tell
them apart and nothing more. Simulated models for a coding task know the reference fix and its
flaws, so `--mock` results validate the harness, not any model's ability to code.

### Frontend and performance tasks: evidence and the agentic judge

"Was the task completed better?" cannot be answered by reading text. For a page or a faster function
the judge needs what a human reviewer would collect: run the tests, lint the code, time it, open the
page. Two categories work that way: 12 `frontend` tasks (landing page, sign-up form, responsive
navigation, dashboard widget, pricing table, FAQ accordion, product grid, dark sign-in card, contact
form, article layout, modal dialog, sortable table) and 12 `performance` tasks (a slow function, a
benchmark harness and the reference timing: deduplicating, counting words, range sums, sorted
intersection, anagram groups, moving average, maximum subarray, breadth-first search, rendering a
log, counting inversions, a prime sieve, two-sum). They are directories like the coding tasks
(`evals/datasets/v1/frontend/<id>/`, `performance/<id>/`, same layout and the same single-patch
answer) with these additions to `task.yaml`:

| Key | Holds |
|-----|-------|
| `category` | `frontend` or `performance` |
| `helpers` | `[sitecheck]` copies the shared test helper (`src/fusion/bench/datasets/helpers/sitecheck.py`) into the hidden tests |
| `hard_gates` | facts that must hold: `{id, evidence, metric?, max?, min?}`; with no metric the evidence's own verdict must be true |
| `soft_criteria` | weighted criteria: `{id, weight, source, description}`, `source` one of `tests`, `static`, `perf`, `a11y`, `visual`, `judge` |
| `perf` | `{script, sizes, samples, warmup, max_ratio, scaling_slack, max_noise}` (performance tasks) |
| `site` | `{entry, timeout_s}`: the page to open (frontend tasks) |
| `evaluators` | the evaluators to run, by name (default: by category) |

The hidden tests of a frontend task are structural (`unittest` over the parsed HTML and CSS: labels,
landmarks, ARIA wiring, a media query, contrast, no skipped heading level); they cannot run
JavaScript or lay anything out. A performance task's hidden tests check behaviour, and its
benchmark script (`tests/bench_workload.py`, hidden too) defines `setup(size)` and `run(state)`.
Slow code is correct code, so a performance task's starter passes the tests and fails the benchmark.

**Evaluators** (`bench/evaluators/`) run on a *copy* of the answer's files and never write to them.
Each returns `Evidence` (`kind`, `ok`, `metrics`, `artifact_path`, `summary`, `cost_usd`, plus `id`,
`seconds` and a `status` of `measured`, `skipped` or `unstable`); judges cite evidence by id.

| Evaluator | Kind | Measures |
|---|---|---|
| `tests` | `tests` | the hidden tests (rerun once to catch flaky tests, results memoised by task and patch), per test; `pass_fraction`, `visible_pass_fraction` |
| `build` | `build` | every Python file compiles; JavaScript passes `node --check` where `node` exists |
| `static` | `static` | ruff (default), mypy, eslint, tsc where installed and named in `static_tools`; cyclomatic complexity of every Python function (read from the AST, so no extra dependency); new third-party imports; secret-looking strings; for pages a viewport meta tag, media queries, the widest fixed width and requests to other hosts |
| `diff_stats` | `diff_stats` | lines and files added, changed, removed |
| `perf` | `perf` | the benchmark at each of `sizes`, in a fresh process each, after a warm-up and at least five samples: median and p90 wall time, noise (MAD over the median), **peak RSS**, the **scaling exponent** (slope of log time on log size, which exposes an O(n²) answer that is fast on a tiny input) and the ratio and scaling excess against the reference solution measured the same way |
| `visual` | `screenshot` | headless Chromium at 390x844 and 1440x900: screenshots (full page and above the fold), horizontal overflow, visible text |
| `console` | `console` | errors the page logged and requests that failed or were blocked |
| `a11y` | `a11y` | violations by impact, contrast failures and a keyboard-focus smoke check |

A `perf` measurement whose noise is above the task's `max_noise` is `unstable`: it carries no verdict
(`ok` is None) and is measured again next time, never scored. Measurements are taken one at a time.
The browser evaluators need the optional extra: `uv sync --extra bench-visual` and
`uv run playwright install chromium` (the extra is `fusion-code-orchestrator[bench-visual]`);
`FUSION_BROWSER_PATH` points at a Chromium or Chrome you already have, instead.
**Without it they are `skipped`**, not failed: a gate that reads their evidence is *unverified*,
which does not fail an answer, and a criterion that needs them is left out of the mean. The
accessibility check then falls back to the same rules read from the HTML source and CSS, which
cannot see what a script adds, so it under-reports and never gives full credit. Inside the browser
the rules are `assets/a11y_rules.js` (a small subset of axe-core's, in axe's result shape); to use
axe-core itself, put its `axe.min.js` next to it (`src/fusion/bench/evaluators/assets/`). Nothing is
fetched from a CDN, ever. How the browser is contained: [SECURITY.md](../SECURITY.md#benchmark-sandbox).

**Gates, criteria and the completion score** (`scoring/completion.py`). Every task has hard gates
(the defaults are "the hidden tests pass" and, for performance, "within the time budget" against
the reference) and weighted soft criteria. The **completion score is 0 if any gate fails, else the
weighted mean of the scored criteria**, so a beautiful page that breaks its tests completes nothing.
A criterion is scored from evidence when it can be (`tests`: pass fraction; `static`: 0.1 per lint
finding, 0.03 per complexity point over 10, 0.5 per secret, 0.15 per new dependency, 0.2 per
request to another host; `a11y`: violations weighted 1 / 0.5 / 0.2 / 0.05 by impact against a
budget of 3; `perf`: full marks within 1.25x the reference, none at 10x, less for a scaling
exponent more than 0.25 above the reference's; `visual`: the share of the page's basic checks that
hold) and by a judge otherwise; those weights are judgement calls, kept in one place, and the
benchmark's numbers move with them. Without judges the criteria only a judge could score
(`design_fidelity`, `ux_polish`) are left out of the mean, not counted as zero, and `bench show
--task ID` lists them as unscored.

**The agentic judge** (`scoring/agentic.py`; used when the study names `--judge-models`). An LLM
that inspects the outputs with read-only tools, then rules:

- **Tools:** `list_files`, `read_file`, `grep`, `view_screenshot` (vision), `get_evidence(kind)`,
  `run_evaluator(name)` (re-runs a *whitelisted* evaluator, at most twice) and `submit_verdict`.
  The providers have no native function calling, so the tools are a JSON protocol that works
  everywhere: each turn the model answers with one `{"tool": ..., "args": ...}` object and gets the
  result as the next message. A **hard step cap** (default 12, the last step must submit) and a
  per-run money cap (`max_usd`, default $0.50 per judge and ordering) end a judge that never
  decides; its criteria then fall back to the measured scores. Every call and result is logged to
  the item's trail (`bench show` counts them; `results.jsonl` holds them).
- **Blind:** outputs are labelled `A`/`B` with a seeded random assignment that is recorded, and
  *both* orderings run, so a judge that names the same position twice is a tie and is reported as
  position bias. The judge sees paths relative to each output, never a local path, an arm or a model.
- **Cross-family, several judges:** a judge from a vendor that serves the arm is left out (the
  arm's members and its named aggregator), at least two must remain, and per-criterion scores are
  averaged with disagreement above 0.3 flagged. Only a vision-capable judge (`supports_vision`) is
  offered screenshots; with none, the verdict says so.
- **Rubric and gates:** the harness, not the judge, computes the hard gates from the evidence. The
  judge scores each soft criterion from 0 to 1 and writes a paragraph that cites evidence ids
  (a verdict that cites none, or scores outside 0 to 1, is rejected and the judge tries again).
- **Untrusted input:** everything derived from a model's answer (files, test output, evidence text)
  arrives inside `<untrusted>` markers, a closing marker inside it is defused, and the judge is told
  never to follow instructions found there. The tools cannot write, run commands or reach the
  network.

**Judge reliability.** `calibrate-judge --artifacts` runs the judges on the validated pairs the
datasets already contain: each task's reference solution against each of its flaws, which are
deliberately broken pages (a missing label, low contrast, a layout that overflows a phone, a fake
modal) and known-slow or fast-but-wrong code. It reports accuracy, ties, position flips, κ and
agreement as the text calibration does, and accuracy per set (`visual`, `perf`).

```bash
uv run fusion bench calibrate-judge --dataset v1 --artifacts --judge-models gemini-flash,gpt-sol --max-usd 1
uv run fusion bench calibrate-judge --dataset v1 --artifacts --mock --cases-per-set 4   # simulated, free
```

A study whose judges scored an artifact gets **no headline verdict** unless each judge's latest
artifact calibration reaches the accuracy floor (default 80%; `FUSION_JUDGE_ACCURACY_FLOOR` or
`--accuracy-floor` on the calibration): `bench run` and `bench show` print "Headline verdict
blocked" and why, and `show --json` carries `judge_gate`. Calibrations of simulated judges only
count for simulated runs.

**What measuring costs.** Evaluator and judge money and seconds are stored as `eval_cost_usd` and
`eval_seconds`, never added to the arm's own cost or latency, and shown in their own columns of
`bench show` so the price of measurement is visible. In a `--mock` study `eval_seconds` is the one
real-time number: the evaluators really run, so it differs between machines and reruns, unlike every
other figure.

**Caveats.** The datasets are synthetic ([provenance](../evals/datasets/README.md#provenance-and-validity-read-this-before-quoting-a-number)).
Hidden frontend tests check structure, not how a page looks; **a screenshot judges visuals, not
intent**, and a judge that sees a pleasant page can still miss a broken interaction no test
covers. Reference screenshots are not shipped (a task's brief is text). Static accessibility rules
are a floor, not an audit, and the in-page ones are a subset of axe-core's. Timings depend on the
machine: the comparison is always against the reference measured in the same process on the same
machine, a noisy measurement is left unscored rather than guessed, and a loaded machine produces
more of them. The default criterion weights and penalties above are choices, not measurements.
Simulated judges read the same evidence a real one would and score it with noise by an assumed
skill; their calibration tests the harness, not any model.

### Suites and the final study

A **suite** is a study kept as a file (`src/fusion/bench/suites/*.yaml`): the `BenchConfig` fields
plus a description and a stage, with arms written as a strategy and overrides. `fusion bench plan
--suite NAME` prices it, `run --suite NAME` runs it, and options on the command line still win
(`--suite headline --mock --repeats 1`). `--suite` also takes the path of a suite file of your own.
A task `quota` (tasks per category, a seeded sample of each; categories not named are left out)
gives every suite a stratified subset that includes the executable, frontend and performance
categories in small numbers, and `spend_stop_usd` stops a suite at $14.50 of the $15 ledger cap,
keeping the reserve. The planner prices a suite's design against what is left below that line.

| Suite | Stage | What it answers | Budget |
|-------|-------|-----------------|--------|
| `ablation` | tune on `dev` | panel size 1/2/3/5, refinement, a strong synthesizer, and the cascade threshold (0.5 and 0.7; more if money remains). The 0.2.0 self-mixture arms (one model sampled at several temperatures) were removed in 0.3.0 | about $4.80 |
| `latency` | tune on `dev` | whether early return, hedging or a cascade shortens the wait; the response cache is off, since a replayed call has no real latency | about $1.20 |
| `headline` | final, on `test` | Opus 5.5, Sol, Haiku and Luna alone against the default panel and the best variant, two repeats | about $11 |
| `ablation-v3` | tune on v2 `dev` | nano, open-weight, mixed and mid panels; lenses on and off; `llm`, `select`, `vote` and `digest` aggregation; the cascade at 0.5 and 0.9; `verified-synth`; `auto` with the frontier rung off and on; 9 tasks, neutral judges | $3.40 |
| `latency-v3` | tune on v2 `dev` | Opus 5.5 alone against `auto`, the panel default and Luna at effort none, on 30 text tasks, cache off | $2.35 |
| `headline-v3` | final, on v2 `test` | Opus 5.5, Sol, Haiku and Luna alone against `auto` and the best ablation variant (a placeholder until Task 16 freezes it), 10 tasks, neutral judges | $1.35 |

The `*-v3` suites are the 0.3.0 study (roadmap Task 16) on dataset v2; the others are the 0.2.0
studies on v1, kept to reproduce them. The v3 caps add up to $7.10 (the planner expects $7.00),
Task 16's share of the $15. They are small because a real-repo coding task shows a whole package
(up to about 50,000 tokens): with the quotas as written a run draws two coding tasks, and one of
them can cost as much as the five text tasks of the same arm. Widen the quotas if the runs come in
under the forecast (the planner has been about three times pessimistic for `auto`).

The procedure that keeps the result honest:

1. **Plan, then run the tuning suites on `dev`** (`ablation`, `latency`): about $6 together. All
   tuning (the cascade threshold, panel size, the synthesizer) happens on these tasks and nowhere else.
2. **Choose, with a stated rule.** `fusion bench recommend RUN` takes the cheapest arm per solved task
   among the cost-quality frontier arms within the margin of the best, says when its interval
   overlaps the best arm's (at this size it usually does: the choice is then a lean, not a finding)
   and prints the `fusion-best` line for the headline suite. If the winner beats the shipped
   default, change the default strategy in `strategies.yaml` and say so in the changelog, even
   if that means Fusion's default is not the panel the project started with.
3. **Freeze, then run `headline` once on `test`.** Do not change an arm afterwards. About $11.
4. **Publish.** `fusion bench results RUN --also ABLATION --also LATENCY --html docs/benchmark-report.html --svg docs/assets/benchmark-cost-quality.svg`
   writes `docs/BENCHMARK_RESULTS.md`: the verdict engine's text unedited, the supporting studies,
   the spend per run and the limitations. Nothing in it is typed by hand.

A suite's estimate comes from the planner, which assumes 1,500-token answers; the live total can be
higher for models that think at length (Opus 5.5 with adaptive thinking), and the caps hold either
way: a run stops cleanly, resumable, when the next job's worst case would not fit. Stopping early
leaves whole tasks for every arm (repeats first, then tasks), so the arms stay paired. **Judges:**
the 0.2.0 suites use `gpt-luna`, whose vendor sits on a panel, for the rubric items the keywords
cannot settle, and their agentic judge of frontend and performance tasks is skipped for panels
(those tasks keep their measured scores); say so whenever quoting one of their scores. The `*-v3`
suites use the [neutral judges](#scoring) MiMo Pro and Muse Ember, from vendors on no arm.

### Tuning the prompts (`fusion tune`)

`fusion tune` improves Fusion's own prompts, the **prompt pack** (`src/fusion/config/prompts/*.md`,
described in [CONFIGURATION.md](CONFIGURATION.md#prompt-pack)), by Harness²'s Propose–Probe–Compose,
on the **dev split only**: the test split is never loaded for probing, so the headline study stays
untouched by tuning.

```bash
fusion tune --mock                                   # free, simulated: shows the procedure
fusion tune --arm auto --limit 24 --max-usd 2        # live: probes are ordinary capped bench runs
fusion tune --mode deep --k 4 --axes inv --max-usd 1
```

1. **Slices.** `--limit` dev tasks (spread over categories) are split, per category and seeded,
   into tuning tasks and a held-out slice (`--holdout`, default 25%).
2. **Probe the base.** The arm (`--arm`, default `auto`) runs on the tuning tasks with the pack in
   use. A probe is an ordinary bench run (`run_bench` with the pack active), so the response
   cache, the run's cap and the live-spend ledger apply to it.
3. **Propose (wide, the default).** For each axis, K proposers (`--k`, default 3) each suggest one
   edit to one prompt of that axis: **INV** (procedure: output shape and verification steps:
   `rules`, `claims`, `select`, `self_check`, the judge and synthesizer roles) or **DOM** (coding
   knowledge per task type: the reviewer, debugger, architect and planner roles, `task.default`).
   A proposer sees the axis's prompts, the base's mean quality and solved share per task type and
   the edits proposed before it; never a task.
4. **Validate, then probe each edit alone.** An edit must name a prompt of its axis, keep every
   `{{placeholder}}` (the untrusted-input rules among them), keep each prompt under 4,000
   characters and the pack's growth under 1,500, and contain no line of grader material (truth
   points, decoys, hidden tests, reference solutions, rubrics, from every split). An invalid edit
   is recorded and never probed; one that leaks is not even shown again.
5. **Compare.** On the tasks both probes scored: the mean per-task quality difference with a
   task-clustered 95% bootstrap interval. The **noise band** is the larger of half that interval
   and `--min-gain` (0.02). Verdict: `helped` (the difference clears the band), `hurt` (it falls
   below minus the band), `no_effect`, or `confounded` (it clearly helped one task type and hurt
   another, or jobs failed so the probes rest on different tasks). Only a task type with at least
   `--min-category-tasks` (2) compared tasks counts here: one task moving either way is noise, and
   the verdict's reason names such task types instead.
6. **Compose.** K composers build finals from the measured edits (the behavioural contrasts):
   composer 1 merges every edit that helped (appends by gain; the best `replace` of a prompt wins),
   the others are model calls that may narrow a `confounded` edit to the task types it helped.
   Finals are validated and probed like proposals.
7. **Keep and confirm.** The best final is kept only if it beats the base by more than the noise
   band; it is then probed once on the held-out slice, with the base, and adopted when it holds up
   there (a positive mean effect that is neither `hurt` nor `confounded`; the slice is too small to
   ask for the band again).

**Deep mode** (`--mode deep`) runs K rounds instead, alternating axes: each round proposes one
edit to the best pack so far and keeps it only if it beats that pack; the result is confirmed on
the held-out slice the same way.

**Output** (`bench-results/tune/<id>/`): `evidence.md` (each edit, its effect with interval and
band, by task type, and its verdict), `pack.diff` (the adopted pack against the base; empty when
nothing was adopted), `pack/` (the adopted pack, for `FUSION_PROMPT_PACK`) and `tune.json`. Nothing
changes in use until you point `FUSION_PROMPT_PACK` at that directory (or copy files into
`config/prompts/`).

**Money.** A live run needs `--max-usd`; it is shared by every probe and every proposer and
composer call, each checked against what is left and against the live-spend ledger (stopping at
$14.50 of the $15 cap). When either would be passed, the run stops cleanly and still writes what
it measured. A run makes at most `1 + axes × K + K + 2` probes (wide) or `K + 3` (deep), and prints
the planner's estimate of one probe before it starts. The proposer and composer default to a
judge-role model (`--tuner-model` names another).

**Simulated runs** cannot learn anything about wording, since a simulated model ignores it. For
`--mock`, the simulator plants effects instead: the simulated proposer appends one of a few fixed
sentences (`providers/simulated_tune.py`) and a panel call whose prompt contains one gets a skill
change on the task types listed with it, so a mock run shows every verdict. **These effects are
made up**: a mock run tests the procedure, never that an edit helps a real model.

### Statistics, reports and how to read them

`fusion bench report RUN` turns a run's items into statistics, verdicts and a report: `--format md`
(default, for a README or docs page), `--format html` (one self-contained file, written to the run's
folder or to `-o PATH`) or `--format json` (the same data, for scripts). `--baseline ARM` picks the
arm the others are compared with (default `solo-opus`, or `solo-frontier` in a 0.2.0 run, when the run has it, else the first arm);
`--margin`, `--min-tasks` and `--resamples` set the verdict rules below. `fusion bench compare
RUN_A RUN_B` compares the same arm across two runs on the tasks both have, paired by task, and
says whether the second is cheaper, faster, not worse or better.

**Intervals.** Every mean has a 95% percentile-bootstrap interval that **resamples tasks, not runs**:
the repeats of one task share a prompt and a truth, so they are not independent, and resampling them
would make the interval too narrow. Five repeats do not narrow it; more tasks do. Intervals are
seeded, so a report is reproducible. With fewer than two tasks there is an estimate and no interval.

**Paired comparisons.** An arm is compared with the baseline on the tasks both were scored on, as
differences per task. Reported: the mean difference with its interval, wins, ties and losses
(a tie is a difference of at most 0.02), the exact two-sided **sign test** over the non-tied tasks,
and an **effect size** (the mean difference over its spread across tasks, Cohen's d for pairs).
Ratios of cost per solved task, cost per task and seconds per task are bootstrapped the same way;
below 1 is better.

**Money and speed.** *$ per solved task* is the arm's whole cost (every item, including failures
and errors, since the money was spent) divided by the items it solved: the headline efficiency
number. An arm that solved nothing has none. *Seconds* are the median and 90th percentile over items
whose wall time is a measurement (cache replays are left out). *Output tokens/s* is effective
(output tokens over wall seconds); decode speed and time to first token are per model and come from
streamed calls. *Tokens per solved* is input plus output tokens over solved items. *Quality per
dollar* is mean quality over cost per task; *quality per minute* is mean quality over mean minutes
per task. *Variance across repeats* is the mean, over tasks, of the standard deviation of quality
across the repeats, and the share of tasks whose solved/unsolved result changes between them: an
arm whose result depends on the repeat needs more repeats before its mean means much. Scoring and
judging (`eval_cost_usd`, `eval_seconds`) are shown beside each arm and never inside its cost or
speed.

**Frontier.** An arm is on the *cost frontier* when no other arm is at least as cheap per task and
at least as good, and strictly better in one; likewise for the *latency frontier* (median seconds).
The HTML charts join the frontier arms with a line and draw each arm's interval as a bar. The
frontier says which arms are worth considering, not that the gaps between them are real: look at
the intervals.

**Verdicts.** For each arm against the baseline, overall and per category, four claims, each
*yes*, *no* or *inconclusive*, with the numbers behind it in the report:

| Claim | yes when | no when |
|-------|----------|---------|
| cheaper | the interval of the cost-per-solved ratio is wholly below 1 | wholly at or above 1 |
| faster | the interval of the seconds-per-task ratio is wholly below 1 | wholly at or above 1 |
| not worse | the interval of the quality difference is above −0.03 (the non-inferiority margin, `--margin`) | below −0.03 |
| better | the interval of the quality difference is wholly above 0 | wholly at or below 0 |

Otherwise the claim is inconclusive: the study cannot tell, which is a result, not a failure. Below
`--min-tasks` (default 10) paired tasks nothing is claimed. The intervals are two-sided 95%, so
"not worse" is a slightly stricter test than the usual one-sided non-inferiority check. Verdicts are
about these tasks, this dataset and these judges; they do not transfer to other work without
repeating the study.

**What blocks a verdict.** When judges scored a frontend or performance task and their latest
artifact calibration is below the accuracy floor (or missing), the headline ("all tasks") verdicts
and those categories' verdicts read *blocked* and the report says why; see
[judge reliability](#frontend-and-performance-tasks-evidence-and-the-agentic-judge).

**Caveats the report prints for itself:** a simulated run (the numbers test the harness, not any
model), a run that did not finish (arms may have answered different tasks), costs that are a lower
bound (an unknown price), errors, cache replays left out of the latency, fewer than three repeats,
and a methodology footer read from today's files because the run has no `meta.json`. The footer
always gives the dataset hash, each arm's strategy and models, each model's id, price and
`verified_on` date, the judge models, repeats, seed, the bootstrap and verdict rules, and the spend.
The HTML report adds a typical run's call timeline per arm, and, for frontend and performance
tasks, the evidence (gates, measurements, the judge's reasoning and screenshots) of the tasks where
an arm and the baseline differed most.

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

`fusion bench` measures Fusion's answers; `fusion bench e2e` measures what matters for cost: whole
Claude Code sessions. It runs headless Claude Code on every executable coding task of a dataset,
each in a sandboxed temporary copy of the task's repo, for executor (Opus, Sonnet, Haiku) x Fusion
(off, on) arms, and records whether the hidden tests pass, wall time, host cost (Claude Code's own
token counts at catalog prices) plus Fusion's cost from its ledger, turns, tool calls and Fusion calls. Results go to the
same store and reports as any study (`fusion bench report` adds a "Claude Code end to end" table).

```bash
uv run fusion bench e2e --mock --limit 4                  # simulated: free, no keys
uv run fusion bench e2e --limit 6 --max-usd 3             # live: needs claude and ANTHROPIC_API_KEY
```

Arms, isolation, every metric and the manual comparison on your own tasks are in
[CLAUDE_CODE_AB.md](CLAUDE_CODE_AB.md).

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
