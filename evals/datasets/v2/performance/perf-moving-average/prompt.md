`moving_average(values, window)` returns the mean of every full window of `window` consecutive values (so `len(values) - window + 1` numbers; none when the window is longer than the list). A window below 1 raises `ValueError`.

```python
def moving_average(values, window):
    """Mean of each full window of ``window`` consecutive values."""
    if window < 1:
        raise ValueError("window must be at least 1")
    return [
        sum(values[start : start + window]) / window
        for start in range(len(values) - window + 1)
    ]
```

Make `moving_average` fast for large inputs (10^5 values with windows of thousands) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
