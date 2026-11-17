---
name: fusion-review
description: Use before committing or merging a non-trivial or security-sensitive diff, or to get an independent check of your own patch. Do not use for typo, formatting, comment-only or one-line changes; review those yourself.
user-invocable: false
---

# Fusion review

Call `fusion_review_diff` with:

- `diff_ref`: the change to review, which Fusion reads itself with a read-only `git diff`: `HEAD`
  for uncommitted work (staged and unstaged), `HEAD~1` for the last commit, `main...HEAD` for a
  branch. Do not paste the diff; send `diff` text only for a change git cannot show
- `paths`: new untracked files (`git diff` leaves them out) and any file the reviewers need beside
  the diff
- `goals`: what to focus on, such as security, concurrency or test gaps
- `context`: what the project is and what the change is for, in a few lines

Check every finding against the code before reporting it. Findings several models report
independently are the strongest; single-model findings may be false positives. Report critical
findings first and say which you confirmed. Fusion reads files but runs nothing: it cannot run
the tests. Paths it could not read (secret, ignored, outside the project) are in the warnings.
