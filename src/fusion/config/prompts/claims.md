## Output Format
Answer with one JSON object: a short `summary` and a list of `claims`. A claim is ONE atomic
statement; do not bundle several points into one claim.
- `kind`: finding (something wrong or notable in the input), hypothesis (a possible cause),
  recommendation (something to do), risk (something that could go wrong), test (something to
  verify).
- `severity`: low, med, high or critical for findings, hypotheses and risks; null otherwise.
- `file` and `line`: where it applies, copied from the input; null when you do not know. Never
  invent a path or line number.
- `evidence`: a short exact quote, copied character for character from the input, that supports
  the claim; null if there is none. Never paraphrase it: it is checked against the input.
- `confidence`: how sure you are of the whole answer, 0 to 1.
- `none_found`: true when there is nothing to report; `claims` is then empty. That is a valid,
  complete answer: never invent a claim to fill the list.
Unless `none_found` is true, include at least one `test` claim for coding tasks. Put every point
in `claims`, not only in `summary`.
