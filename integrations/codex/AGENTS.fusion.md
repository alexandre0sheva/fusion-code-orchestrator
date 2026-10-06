## Fusion: a second opinion from a panel of cheap models

The `fusion` MCP server asks several cheap models in parallel and returns one answer with the
points they agree on. It sees only the text you send and returns text: it reads no files, runs
nothing and edits nothing. You stay the executor.

Call it when a decision is hard or costly to get wrong:

- a diff is non-trivial or risky (security, concurrency, migrations, public APIs): `fusion_review_diff`
- an error is not explained by its message, or a fix did not work: `fusion_debug_error`
- a choice is hard to reverse (storage, queues, framework, service boundaries): `fusion_decide_architecture`
- a feature spans several files and the approach is unclear: `fusion_plan_feature`
- you are unsure and a wrong answer would be costly: `fusion_ask`

Do not call it for one-line fixes, typos, formatting, renames, anything you can read and judge
directly, or anything that needs files read or commands run (do that first, then send what matters).

Send focused input: the diff, or the error with its stack trace and the code around it, plus a few
lines of `context`. A call takes tens of seconds and costs cents; `strategy: "solo-cheap"` is the
quickest and `max_cost_usd` caps the spend. Claims several models agree on are the most reliable;
check single-model claims against the code. If the response has `partial: true`, it hit its time
limit and is the panel's digest. Verify before applying: Fusion's answer is another model's opinion.
