# Evaluation datasets

Two kinds live here.

- **`v1/`: the ground-truth benchmark dataset** that `fusion bench` studies run on. Methodology,
  scoring and commands are in [docs/BENCHMARKING.md](../../docs/BENCHMARKING.md#datasets).
- `*_cases.jsonl`: the older, unscored cases used by `evals/runners/run_offline_eval.py`.

## `v1` at a glance

100 tasks: 25 each of code review, debugging, architecture decisions and implementation
planning, in easy, medium and hard, in Python, TypeScript and Go where code is involved
(review and debugging). Each task is in exactly one split: `dev` (15 per category, for tuning
thresholds and prompts) or `test` (10 per category, touched only by the final study).

```bash
uv run fusion bench dataset validate evals/datasets/v1 --release   # checks everything below
uv run fusion bench dataset stats evals/datasets/v1
uv run fusion bench dataset build --check                          # JSONL matches its sources
```

## Provenance and validity: read this before quoting a number

**Every task is synthetic.** They were written by an LLM (Claude), not collected from real
repositories, incidents or design reviews, and have **not yet been independently reviewed by a
person** (`llm-authored` is in every task's tags; reviewers remove no tag, they edit the source and
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

## Licence

The dataset, its authoring sources and these notes are released under the repository's
[MIT licence](../../LICENSE) (copyright Oleksandr Shevchenko). The tasks contain only original,
synthetic code and prose written for this project: no third-party code, data or text was copied
into them.
