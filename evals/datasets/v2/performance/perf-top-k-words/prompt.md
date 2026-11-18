`top_k_words(words, k)` returns the `k` most frequent words, most frequent first. Words with the same count come in alphabetical order. With fewer than `k` distinct words it returns all of them; with `k` of zero or less it returns an empty list.

```python
def top_k_words(words, k):
        """The k most frequent words, most frequent first, ties in alphabetical order."""
        distinct = []
        for word in words:
            if word not in distinct:
                distinct.append(word)
        ranked = sorted(distinct, key=lambda word: (-words.count(word), word))
        return ranked[:max(k, 0)]
```

Make `top_k_words` fast for large inputs (10^5 items) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
