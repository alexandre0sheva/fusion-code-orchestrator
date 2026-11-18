# Evaluation datasets

Canonical for where the benchmark data comes from: provenance, validity limits, licence and the
guidelines for writing tasks.

Three kinds live here.

- **`v2/`: the ground-truth benchmark dataset** the 0.3.0 studies run on (the `*-v3` suites).
  Methodology, scoring and commands are in [docs/BENCHMARKING.md](../../docs/BENCHMARKING.md#datasets).
  It holds every `v1` task with its id and split, three more frontend and three more performance
  tasks, and [real-repo coding tasks](#dataset-d-real-repo-coding-tasks-v2): upstream bugs with
  their fix and tests. [Below](#v2-at-a-glance).
- **`v1/`: the 0.2.0 dataset**, kept as it was for the 0.2.0 results. It has three parts: text tasks
  scored by keywords and judges (Dataset A), [executable coding tasks](#dataset-b-executable-coding-tasks)
  scored by running tests (`v1/coding/`) and [frontend and performance tasks](#dataset-c-frontend-and-performance-tasks)
  scored on what the answer builds (`v1/frontend/`, `v1/performance/`).
- `*_cases.jsonl`: the older, unscored cases used by `evals/runners/run_offline_eval.py`.

## `v2` at a glance

197 tasks: Datasets A to C as in `v1` (100 text tasks, 33 synthetic coding tasks), 15 frontend and
15 performance tasks (`fe-not-found`, `fe-settings-tabs`, `fe-photo-gallery`, `perf-count-pairs-diff`,
`perf-top-k-words` and `perf-longest-unique-run` are new), and 34 real-repo coding tasks from 15
open-source projects (Dataset D), so 67 coding tasks in all. Every category has at least 15 tasks.
Three files beside the tasks (a file whose name starts with `_` is never read as a task):

- `_dataset.yaml`: the release promise `validate --release` holds the dataset to (at least 120
  tasks, 15 per category, 30 real-repo tasks with 12 in each split, among others).
- `_splits.json`: the split, **frozen on 2026-11-18 before any live run** (108 dev, 89 test;
  real-repo tasks 18 dev, 16 test). A task that changes split, or is not in the file, fails
  validation; `fusion bench dataset freeze --add` takes in a new task with the split it declares.
  `v1` tasks keep their split, so nothing tuned in 0.2.0 or 0.3.0 moved to `test`.
- `_review.yaml`: the [owner's review](#the-owners-review) of every task.

```bash
uv run fusion bench dataset validate evals/datasets/v2 --release   # runs every executable task (minutes)
uv run fusion bench dataset stats evals/datasets/v2
uv run fusion bench dataset build --check                          # v2 JSONL matches authoring/v2
uv run fusion bench dataset review evals/datasets/v2               # refresh the review sheet, show progress
```

## The owner's review

No task of `v1` was reviewed by a person. `v2/_review.yaml` lists every task with a `decision`
(`pending`, `accept`, `fix`, `drop`) and a `note`; the owner fills it in, using the guidelines in
this file. `fusion bench dataset review` adds new tasks as `pending` and never changes a decision.
`validate` warns while tasks are pending or marked `fix` (the note says what to fix), and fails
`--release` while a task marked `drop` is still in the dataset; once it is deleted its entry stays
as the record. Until the sheet is complete, every caveat below about unreviewed tasks holds.

## `v1` at a glance

Dataset A: 100 tasks: 25 each of code review, debugging, architecture decisions and implementation
planning, in easy, medium and hard, in Python, TypeScript and Go where code is involved
(review and debugging). Dataset B: 33 executable coding tasks in Python. Dataset C: 12 frontend tasks
(HTML, CSS and JavaScript) and 12 performance tasks (Python). Each task is in exactly one split:
`dev` (15 per text category, 16 coding tasks and 6 each of frontend and performance, for tuning
thresholds and prompts) or `test` (10 per text category, 17 coding tasks and 6 each of frontend and
performance, touched only by the final study).

```bash
uv run fusion bench dataset validate evals/datasets/v1 --release   # checks everything below
uv run fusion bench dataset stats evals/datasets/v1
uv run fusion bench dataset build --check                          # JSONL matches its sources
```

## Provenance and validity: read this before quoting a number

**Every task except the real-repo coding tasks is synthetic.** They were written by an LLM
(Claude), not collected from real repositories, incidents or design reviews, and have **not yet been
independently reviewed by a person** (for `v2`, [the owner's review](#the-owners-review) is where
that happens) (`llm-authored` is in every task's tags; reviewers remove no tag, they edit the source and
record the review in the change). What that implies:

- Bugs, traces and logs are plausible, not observed. A seeded defect is real in the code shown, but
  real defects are messier, and real diffs contain more unrelated code.
- Code review diffs are small (30 to 60 lines) and each seeds one or two defects. A review with a
  clean diff tests false alarms; the dataset has six clean reviews (24%).
- The authors of tasks and of the models under test share training data and style. A task
  that "sounds like a model wrote it" may favour model-written answers. The rubric points are written
  as concepts with alternative wordings to limit this, but keyword matching still rewards wording the
  author anticipated.
- Architecture and planning rubrics encode one author's judgement of what a good answer covers.
  Several valid designs exist for most; rubrics avoid requiring a product unless the constraints
  force it, and the judge-based checks are a second opinion, not a gold standard.
- Difficulty labels are the author's estimate, not measured (the study's own results are the
  measurement).

Treat results on this dataset as evidence about *this* benchmark. Do not present them as evidence
about all engineering tasks.

## Where the tasks come from

`authoring/v1/*.yaml` are the sources people read and edit; `v1/*.jsonl` is compiled from them by
`fusion bench dataset build` (a test fails if the two differ). The authoring formats are documented
at the top of `src/fusion/bench/datasets/build.py`. In short:

- **Code review**: each file is a diff whose lines start with `=` (unchanged), `+` (added) or `-`
  (removed); write `«b1»` at the end of the added line a bug is on and describe it under `bugs`. The
  compiler computes line numbers, so they cannot drift. No `bugs` makes a clean change.
- **Debugging**: the code, the failure output, the logs, the root cause (equivalent `root_cause_tags`
  plus wordings in `root_cause_aliases`) and the fix (`fix_keywords`, where `a|b` accepts either).
- **Architecture and planning**: a prompt, a context, `required` points (keywords are alternative
  wordings; `gate: true` on the one that decides correctness) and `forbidden` points.

### Guidelines for adding or reviewing a task

1. One clear ground truth. A review task seeds exactly the listed defects and no other: if a careful
   reviewer could report something else as a bug, either fix the code or list it.
2. Make the task answerable from what is shown. Nothing in the truth may depend on facts not in the
   prompt, context or files.
3. Keywords are alternatives, in the stem form people write (`idempoten`, `rollback`), and each point
   has at least three. Prefer concepts over product names unless the constraints force one.
4. No secrets, real people or real companies. Use `example.com` and placeholders.
5. Run `fusion bench dataset validate evals/datasets/v1 --release`. Among other things it checks
   that an answer written from the task's own truth scores at least 0.9 and a flawed one at most 0.5
   with the offline scorers. That catches a truth that contradicts itself (a bug line that is not an
   added line, an alias that does not name its category); it cannot tell whether real answers will
   use your keywords, which only a judge-based run on a few model answers shows.
6. Add the task to `dev` or `test`, never both; the validator rejects shared prompts and file sets.

`fusion bench dataset build --generate N --category C --model ALIAS --max-usd X` asks a model to
draft candidates into `authoring/candidates/` for a person to review; nothing generated is used
until a person has checked every seeded defect and rubric point and moved it into `authoring/v1/`.
Generation costs money and is forecast, capped and recorded in the spend ledger.

## Dataset B: executable coding tasks

`v1/coding/<task_id>/` holds 33 small Python tasks (standard library only) that a patch is scored
on by running **hidden tests**: 16 implement a function or class from a written spec, 17 fix bugs
in a small repository, one or three files each, 10 easy, 18 medium and 5 hard. How they are run,
scored and sandboxed is in [docs/BENCHMARKING.md](../../docs/BENCHMARKING.md#coding-tasks-and-the-sandbox).

```
<task_id>/
  task.yaml            id, difficulty, split, tags, solution_summary, flaws, expected_pass
  prompt.md            the request (the answer-format instruction is appended when it loads)
  repo/                the files the model sees, including a few visible tests in repo/tests/
  tests/               the hidden tests (name them test_hidden.py; the task's visible ones are test_visible.py)
  reference/solution/  the changed files of a correct fix, as whole new files
  reference/flaw-N/    the changed files of the N-th plausible wrong fix (one summary each in task.yaml)
```

The directories are the source of truth; there is no compile step. `fusion bench dataset validate
evals/datasets/v1/coding` loads every task and proves it sound by running it: the reference
solution passes every hidden test, twice; the unpatched files fail some; every flaw applies and
fails some; hidden files do not overwrite visible ones; each task has visible tests.

**Provenance and validity.** Like Dataset A, every task is synthetic, written by an LLM (Claude) and
not yet reviewed by a person (`llm-authored` is in every task's tags). What that implies for coding
tasks:

- They are small and self-contained: an implementation of one function or class, or a bug in tens of
  lines. They say little about changes in a large, unfamiliar codebase.
- The hidden tests encode what the prompt says, written by the same author as the prompt. A correct
  patch that reads the prompt differently fails, and a patch that satisfies the tests can still be
  poor code (nothing here scores style, performance or safety).
- Each task has a reference fix and two wrong ones. That proves the tests can tell those apart, not
  that they catch every wrong patch.
- Tests were not mutation-tested. The visible tests are lifted from the hidden ones and, on
  purpose, catch roughly two thirds of the wrong fixes: that is what the `best-of-n-verified` arm
  measures.
- Simulated models know the reference fix and flaws; `--mock` results test the harness only.

### Guidelines for adding or reviewing a coding task

1. Everything a test checks must be stated in `prompt.md` (names, signatures, error types, edge
   cases). If a careful engineer could reasonably return something else, specify it or drop the test.
2. Standard library only, deterministic, no network, no clock or randomness the test does not control
   (inject a clock), no printing from tests (the runner reads `unittest -v` output), no docstrings on
   test methods, and finishing in about a second.
3. Write 6 to 12 hidden tests that each check one behaviour, with names that say which. List their
   ids in `expected_pass` (`tests.test_hidden.Class.test_name`); the validator rejects an id that
   the solution does not pass.
4. Give two flaws that a plausible model would write: partial fixes, the fix for the reported symptom
   only, a missed edge case. A flaw must apply and must fail at least one hidden test.
5. Put two or three visible tests in `repo/tests/test_visible.py`: the examples, plus a reproduction
   of the reported problem for a bug-fix task. They must pass with the solution.
6. No secrets, real people or companies. Run the validator, and add the task to `dev` or `test`,
   never both.

## Dataset C: frontend and performance tasks

`v1/frontend/<task_id>/` (12 tasks) and `v1/performance/<task_id>/` (12) use the directory layout of
the coding tasks, with the keys `category`, `helpers`, `hard_gates`, `soft_criteria`, `perf` and `site`
in `task.yaml` ([the table](../../docs/BENCHMARKING.md#frontend-and-performance-tasks-evidence-and-the-agentic-judge)).
They are scored by evaluators that run tests, lint, time the code and open the page, and by an
agentic judge; the methodology, the evaluators and their limits are in
[docs/BENCHMARKING.md](../../docs/BENCHMARKING.md#frontend-and-performance-tasks-evidence-and-the-agentic-judge).

- **Frontend:** a design brief and an empty page (`index.html` with a TODO). The answer is HTML, CSS
  and JavaScript, offline, in one patch. Hidden tests (`tests/test_hidden.py`, using the shared
  `sitecheck` helper) check structure, accessibility and responsiveness from the parsed source.
  Reference screenshots are not shipped: the brief is text.
- **Performance:** slow but correct code, visible tests, hidden behavioural tests and a hidden
  benchmark (`tests/bench_workload.py`: `setup(size)`, `run(state)`). The reference solution is the
  yardstick: an answer must be within 3x of it at the largest size and scale no worse than 0.5 above
  its exponent.
- **Each task's two flaws.** For a page: a broken one (the structure, labels or responsiveness the
  brief asks for are missing) and one that passes every hidden test but is measurably worse (low
  contrast, a positive tabindex, a skipped heading, a stylesheet fetched from another host). For
  code: a correct but still slow solution and a fast but wrong one that passes the visible tests.
  These are what the judge calibration uses.

`fusion bench dataset validate` proves each task sound by measuring it: the reference solution
passes every gate and completes at least 0.85; the unpatched files do not (for a page: they fail the
gates; for performance: they pass the tests and fail the benchmark, because slow code is correct
code); every flaw fails a gate or, for a page, scores at least 0.04 below the solution. A timing too
noisy to decide is a warning.

**Provenance and validity.** Synthetic, written by an LLM (Claude) and not yet reviewed by a person
(`llm-authored` in every tag list). Besides the cautions about the other datasets:

- Frontend tests read source; they cannot run JavaScript, so a script that breaks at run time passes
  them. A screenshot judges the look, not the intent, and no screenshots exist without Playwright.
- The pages are small and the briefs say what to build. Real front-end work has a design system,
  existing code and a browser matrix.
- Performance tasks are classic algorithm exercises where a data structure decides the answer; they
  say little about profiling a real service. Timings are relative to the reference on the same
  machine, which is also why a very loaded machine yields `unstable` measurements.
- Simulated models know the reference fix and its flaws: `--mock` results test the harness only.

### Guidelines for adding or reviewing a frontend or performance task

1. State in the brief or the prompt everything a test checks (names, structure, behaviour, the
   shape of the answer). A page test that a reasonable page would fail is a bug in the test.
2. A frontend test checks one thing and names it; use the `Page` helper, which does not run
   scripts. Pages must pass their own hidden tests *and* the static accessibility rules (the
   validator runs both).
3. A performance task needs a workload where the reference is at least a few times faster than the
   starter at the largest size and a different complexity class from it, with sizes whose slowest
   variant takes well under a second, so that a measurement is not noise. Check the noise with
   `fusion bench dataset validate`.
4. Give two flaws as above. Each must apply and be caught: by a failing test or gate, or (pages
   only) by a measurably lower completion score.
5. Offline, no secrets, no real people or companies. Add the task to `dev` or `test`, never both,
   with its own starter file contents (the validator rejects file sets shared between splits).

## Dataset D: real-repo coding tasks (v2)

`v2/coding/real-*/` holds 34 bugs fixed upstream in small, permissively licensed, pure-Python
projects. Each is the buggy state of the project (the fix commit's parent) and the fix, in the
coding-task layout above, with these differences:

- `task.yaml` says `provenance: real-repo` and has a `source` block: `repo`, `url`, the fixing
  `commit` and its `parent` (full hashes), `committed` (date), `licence` (SPDX), the vendored
  `upstream_files`, the `upstream_test` and, where something had to stand in for a file the
  upstream build generates, `adapted`. `LICENSE` beside it is the project's licence text; the model
  does not see it. Tags are `real-repo` and `repo:<name>` instead of `synthetic` and `llm-authored`.
- `repo/` is the project's package (or the modules the tests need) at the parent commit, plus the
  parent's version of the test file the fix changed (the visible tests) and any test helpers it
  imports. `reference/solution/` is the fix commit's version of the source files it changed.
- The hidden tests are the **upstream test file at the fix commit** (`tests/test_hidden_<name>.py`),
  run with pytest over `tests/`. `expected_pass` is every hidden test that passes with the fix in
  the bench sandbox: the ones the fix makes pass (`fail_to_pass`, recorded in `task.yaml`, 79 in
  all) and the ones that passed before and still must (pass-to-pass). A task counts as solved only
  when all of them pass, as in SWE-bench. Upstream tests that fail even with the fix in the
  sandbox (they need data files, `numpy` or another package that is not installed) are left out.
- `prompt.md` is a bug report written for this project (by Claude, from the commit, its message and
  its tests): the symptom, an example and what must keep working, never the fix. Examples are not
  copied from the hidden tests (`validate` rejects a prompt that repeats a hidden line). No issue
  text was copied.
- There are no flaws (no plausible wrong fixes); `validate` does not ask for them here.

| Project | Licence | Tasks | Fix commits |
|---|---|---|---|
| [boltons](https://github.com/mahmoud/boltons) | BSD-3-Clause | 1 | 2026-09-07 |
| [cachetools](https://github.com/tkem/cachetools) | MIT | 3 | 2026-06-24 to 2026-11-15 |
| [dacite](https://github.com/konradhalas/dacite) | MIT | 3 | 2020-11-27 to 2022-12-21 |
| [docstring_parser](https://github.com/rr-/docstring_parser) | MIT | 2 | 2021-07-21 to 2026-06-30 |
| [funcy](https://github.com/Suor/funcy) | BSD-3-Clause | 1 | 2026-09-09 |
| [humanize](https://github.com/python-humanize/humanize) | MIT | 4 | 2026-06-30 to 2026-09-16 |
| [inflection](https://github.com/jpvanhal/inflection) | MIT | 2 | 2020-04-06 to 2020-04-07 |
| [markupsafe](https://github.com/pallets/markupsafe) | BSD-3-Clause | 1 | 2021-06-11 |
| [parse](https://github.com/r1chardj0n3s/parse) | MIT | 4 | 2026-02-18 to 2026-10-27 |
| [python-semver](https://github.com/python-semver/python-semver) | BSD-3-Clause | 2 | 2026-07-15 to 2026-10-05 |
| [python-tabulate](https://github.com/astanin/python-tabulate) | MIT | 1 | 2026-03-05 |
| [sqlparse](https://github.com/andialbrecht/sqlparse) | BSD-3-Clause | 3 | 2025-11-29 to 2026-07-22 |
| [toml](https://github.com/uiri/toml) | MIT | 2 | 2018-09-14 to 2018-12-31 |
| [toolz](https://github.com/pytoolz/toolz) | BSD-3-Clause | 1 | 2026-09-18 |
| [validators](https://github.com/python-validators/validators) | MIT | 4 | 2026-06-19 to 2026-10-10 |

**Validity.** These are real defects in real code, which is the point, but read results with care:

- **Contamination.** The projects are public; 24 of the 34 fixes are from 2026, the rest are older
  and may be in a model's training data, fix included. Compare models on the recent ones too.
- **Size.** A task shows a whole small package (about 600 to 4,900 lines, up to about 50,000 tokens), so a
  real-repo task costs several text tasks to run. They are still small projects with one-file fixes,
  not large unfamiliar codebases.
- **The tests decide.** The hidden tests are the maintainers' own; a different correct fix can fail
  a test that encodes the upstream choice (an error type, a message). The prompts state what the
  tests check that a careful reader could not infer, but that is a judgement.
- **Picked by hand.** Fixes were chosen for being self-contained, testable with pytest alone and
  small; that selection favours easy-to-specify bugs.

### Guidelines for adding or reviewing a real-repo task

1. Pick a fix commit that changes one or two source files and a test file, in a project under a
   permissive licence whose tests need nothing beyond pytest and the standard library.
2. Vendor the parent's package, the parent's version of the test file and its helpers; write the fix
   commit's test file as `tests/test_hidden_<name>.py` and the fix as `reference/solution/`; copy
   `LICENSE`; fill in `source`.
3. Run the hidden tests in the sandbox without and with the fix: keep the tests that pass with it as
   `expected_pass` and require at least one in `fail_to_pass`.
4. Write the bug report: symptom, a reproduction with values of your own, what must not change.
5. Run `fusion bench dataset validate`, add the task to the review sheet and, if the split is
   frozen, to `_splits.json` with `freeze --add`.

## Licence

The dataset's own material (every synthetic task, the authoring sources, the real-repo bug reports
and these notes) is released under the repository's [MIT licence](../../LICENSE) (copyright
Oleksandr Shevchenko). The real-repo tasks (`v2/coding/real-*/`) also contain third-party code and
tests, vendored unchanged: each task's files are under the licence of their project, MIT or
BSD-3-Clause (table above), whose text and copyright notice are in that task's `LICENSE`.
