---
name: fusion-orchestrator
description: Use when a coding decision is hard or costly to get wrong and a second opinion from several cheap models would help. Do not use for trivial edits, renames, formatting, reading or searching files, or anything you can answer from the code in front of you.
user-invocable: false
---

# Fusion: when to ask the panel

Fusion asks a panel of cheap models in parallel and returns one answer with the points they agree
on. It sees only the text you send and returns text: it reads no files, runs nothing and edits
nothing. You stay the executor.

## Call it when

- a diff is non-trivial or risky (security, concurrency, migrations, public APIs): `fusion_review_diff`
- an error is not explained by its message, or a fix did not work: `fusion_debug_error`
- a choice is hard to reverse (storage, queues, framework, service boundaries): `fusion_decide_architecture`
- a feature spans several files and the approach is unclear: `fusion_plan_feature`
- you are unsure and a wrong answer would be costly: `fusion_ask`
- you are about to rely on a draft answer: `fusion_eval_answer`

## Do not call it for

- one-line fixes, typos, formatting, renames, or code you can read and judge directly
- anything that needs files read or commands run: do that yourself first, then send what matters
- a question you already answered with high confidence

## How to use the answer

- Send focused input: the diff, or the error with its stack trace and the code around it, plus a
  few lines of `context`. Short excerpts in `file_snippets`, each prefixed with its path.
- A call takes tens of seconds and costs cents. `strategy: "solo-cheap"` is the quickest and
  cheapest; `max_cost_usd` caps the spend. The response states the cost.
- The reply is compact: the answer, the top claims, confidence and one cost line. Claims backed by
  several models are the most reliable; treat single-model claims as leads and check them against
  the code. Confidence under 0.5 with `low_information` means only one model answered, so nothing
  was cross-checked. `detail: "full"` returns everything; a `run_id` can be read later at
  `fusion://runs/{run_id}`.
- With `strategy: "panel-digest"` no model merges the answers, so you are the aggregator.
- If the response has `partial: true`, the call hit its time limit and this is the panel's digest.
- Verify before applying: Fusion's answer is another model's opinion.
