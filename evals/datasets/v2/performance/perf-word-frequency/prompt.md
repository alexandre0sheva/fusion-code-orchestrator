`top_words(text, k)` returns the `k` most frequent words of a text as `(word, count)` pairs, most frequent first. Words are runs of letters and apostrophes, compared ignoring case; equal counts are ordered alphabetically.

```python
import re


def top_words(text, k):
    """The ``k`` most frequent words as (word, count), ties in alphabetical order."""
    words = re.findall(r"[a-z']+", text.lower())
    counts = {}
    for word in set(words):
        counts[word] = words.count(word)
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return ranked[:k]
```

Make `top_words` fast for large inputs (texts of a million words) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
