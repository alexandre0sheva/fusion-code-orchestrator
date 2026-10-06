`two_sum(nums, target)` finds two different positions whose values add up to `target`. It scans the positions `j` from the left and returns `(i, j)` for the first `j` that has an earlier position `i` with `nums[i] + nums[j] == target`, using the smallest such `i`. It returns `None` when there is no such pair.

```python
def two_sum(nums, target):
    """Indices (i, j), i < j, of the first pair adding up to ``target``, or None."""
    for j in range(len(nums)):
        for i in range(j):
            if nums[i] + nums[j] == target:
                return (i, j)
    return None
```

Make `two_sum` fast for large inputs (lists of 10^5 numbers) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
