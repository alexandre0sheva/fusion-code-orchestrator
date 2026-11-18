`range_sums(values, queries)` answers many range-sum queries. Each query is a pair `(l, r)` meaning the sum of `values[l:r]` (end exclusive). Bounds outside the list are clamped to it, and a query with `l >= r` sums to 0.

```python
def range_sums(values, queries):
    """The sum of values[l:r] for each (l, r) in ``queries``, bounds clamped, empty ranges 0."""
    answers = []
    for left, right in queries:
        left = max(left, 0)
        right = min(right, len(values))
        answers.append(sum(values[left:right]) if left < right else 0)
    return answers
```

Make `range_sums` fast for large inputs (10^5 values and 10^5 queries) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
