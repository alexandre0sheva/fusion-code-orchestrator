`max_subarray_sum(values)` returns the largest sum of a non-empty run of consecutive values. An empty list raises `ValueError`.

```python
def max_subarray_sum(values):
    """Largest sum of a non-empty contiguous run of ``values``."""
    if not values:
        raise ValueError("values must not be empty")
    best = values[0]
    for start in range(len(values)):
        total = 0
        for end in range(start, len(values)):
            total += values[end]
            if total > best:
                best = total
    return best
```

Make `max_subarray_sum` fast for large inputs (lists of 10^5 numbers) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
