`intersect_sorted(a, b)` takes two lists sorted in ascending order (they may contain repeats) and returns the sorted list of values present in both, each value once.

```python
def intersect_sorted(a, b):
    """Values present in both sorted lists, each once, ascending."""
    common = []
    for value in a:
        if value in b and value not in common:
            common.append(value)
    return common
```

Make `intersect_sorted` fast for large inputs (lists of 10^5 values) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
