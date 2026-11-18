`count_pairs(nums, diff)` counts the index pairs `(i, j)` with `i < j` and `nums[j] - nums[i] == diff`. Order matters: a pair counts only when the later value minus the earlier one equals `diff`, which may be zero or negative. Equal values at different positions are different pairs.

```python
def count_pairs(nums, diff):
        """Number of index pairs i < j with nums[j] - nums[i] == diff."""
        count = 0
        for j in range(len(nums)):
            for i in range(j):
                if nums[j] - nums[i] == diff:
                    count += 1
        return count
```

Make `count_pairs` fast for large inputs (10^5 items) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
