---
name: fusion-eval
description: Use to score a draft answer or explanation against its question and a rubric before relying on it. Do not use to find out whether code works or a fact is true; it judges text only and runs nothing.
user-invocable: false
---

# Fusion eval

Call `fusion_eval_answer` with the `question`, the `answer`, the `context` it was produced from,
and `expected_criteria` (or a `rubric`).

Report the score, the unsupported claims and the missing points. A low score means the answer needs
work, not that it is wrong: verify against the code or run the tests.
