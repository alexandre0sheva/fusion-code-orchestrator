`unique(items)` returns the distinct items of a list in the order they first appear. Items are hashable. It is called on event streams with hundreds of thousands of items.

```python
def unique(items):
    """Return the distinct items of ``items``, in order of first appearance."""
    result = []
    for item in items:
        if item not in result:
            result.append(item)
    return result
```

Make `unique` fast for large inputs (lists of 10^5 to 10^6 items) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
