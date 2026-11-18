`count_inversions(values)` counts the pairs of positions `i < j` with `values[i] > values[j]` (strictly greater: equal values are not inversions). It is used to measure how far a ranking is from sorted order.

```python
def count_inversions(values):
    """Number of pairs i < j with values[i] > values[j]."""
    count = 0
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            if values[i] > values[j]:
                count += 1
    return count
```

Make `count_inversions` fast for large inputs (lists of 10^5 numbers) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
