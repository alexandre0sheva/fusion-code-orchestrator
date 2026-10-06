`top_words(text, n)` in `words.py` counts words, but "The" and "the" are counted separately, "fox." is a different word from "fox", and ties come out in no sensible order.

Fix it to behave as documented:

- A word is a run of letters `a-z` (case-insensitive), optionally with apostrophes between letters, so `don't` is one word. Digits and other punctuation separate words and are never part of one. Words are lower-cased.
- Return the `n` most frequent words as a list of `(word, count)` tuples, highest count first; words with the same count are sorted alphabetically.
- `n` of 0 gives `[]`; a negative `n` raises `ValueError`; an `n` larger than the number of distinct words returns them all.
