The `retry` decorator in `retry.py` is meant to retry a flaky function with exponential backoff. Two problems are reported: callers wait for a pointless sleep after the **last** failed attempt before the error finally appears, and a decorated function loses its `__name__` and docstring.

Make it behave as documented:

- `retry(attempts=3, delay=1.0, backoff=2.0, exceptions=(Exception,), sleep=time.sleep)` returns a decorator. The decorated function is called up to `attempts` times in total.
- After a failure with one of `exceptions` it calls `sleep(wait)` and tries again; `wait` starts at `delay` and is multiplied by `backoff` after each wait. There is **no** sleep after the final failed attempt: the last exception is re-raised at once.
- An exception that is not in `exceptions` propagates immediately with no sleep. A successful call returns the function's value immediately.
- The decorated function keeps the original `__name__` and `__doc__`.
- `attempts` below 1 raises `ValueError` when `retry(...)` is called.
