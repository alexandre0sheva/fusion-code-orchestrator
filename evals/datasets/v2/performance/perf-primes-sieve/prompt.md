`primes_up_to(n)` returns every prime number up to and including `n`, in ascending order. For `n` below 2 it returns an empty list.

```python
def primes_up_to(n):
    """All primes p with 2 <= p <= n, ascending."""
    return [
        candidate
        for candidate in range(2, n + 1)
        if all(candidate % divisor for divisor in range(2, int(candidate**0.5) + 1))
    ]
```

Make `primes_up_to` fast for large inputs (n up to 10^7) without changing what it returns. A hidden test suite
checks the behaviour (including the edge cases the description above implies) and a benchmark times
the function on inputs of several sizes, so a solution that only speeds up small inputs does not pass.
Keep the function name and signature. Python 3.12 standard library only.
