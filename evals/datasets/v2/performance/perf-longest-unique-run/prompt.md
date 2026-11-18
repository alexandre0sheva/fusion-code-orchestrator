`longest_unique_run(text)` returns the longest substring of `text` in which no character repeats. When several are equally long it returns the one that starts first; for an empty string it returns `""`. Any character counts, including spaces and non-ASCII letters.

```python
def longest_unique_run(text):
        """The longest substring without a repeated character (the first one on ties)."""
        best = ""
        for start in range(len(text)):
            seen = set()
            end = start
            while end < len(text) and text[end] not in seen:
                seen.add(text[end])
                end += 1
            if end - start > len(best):
                best = text[start:end]
        return best
```

Make `longest_unique_run` fast for large inputs (10^5 items) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
