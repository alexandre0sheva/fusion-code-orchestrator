---
name: fusion-review
description: Use before committing or merging a non-trivial or security-sensitive diff, or to get an independent check of your own patch. Do not use for typo, formatting, comment-only or one-line changes; review those yourself.
user-invocable: false
---

# Fusion review

Call `fusion_review_diff` with:

- `diff`: the unified diff (`git diff HEAD`)
- `changed_files`: the changed paths
- `goals`: what to focus on, such as security, concurrency or test gaps
- `context`: what the project is and what the change is for, in a few lines

Check every finding against the code before reporting it. Findings several models report
independently are the strongest; single-model findings may be false positives. Report critical
findings first and say which you confirmed. Fusion reviews text only: it cannot run the tests.
