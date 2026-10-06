Implement the class `TokenBucket` in `bucket.py`, a rate limiter.

- `TokenBucket(rate, capacity, clock)`: the bucket holds at most `capacity` tokens and starts full. It gains `rate` tokens per second continuously (fractions count). `clock` is a function returning the current time in seconds; use it, never the real time. `rate` and `capacity` must be positive, else `ValueError`.
- `allow(cost=1)` takes `cost` tokens and returns `True` if that many are available, otherwise takes nothing and returns `False`. A `cost` that is not positive, or larger than `capacity`, raises `ValueError`.
- `wait_time(cost=1)` returns the number of seconds until `cost` tokens are available (`0.0` if they are now). It takes nothing.
- `tokens()` returns the current number of tokens as a float after refilling.
