`TTLCache` in `ttlcache.py` stores values that expire after `ttl` seconds, using an injected `clock`. Reported problems: an entry is still returned at the exact moment its time is up; `len(cache)` keeps counting expired entries; and `key in cache` is `True` for an expired key.

Fix it so that:

- An entry set at time `t` is live while `now < t + ttl` and expired from `t + ttl` on. Setting an existing key restarts its time.
- `get(key, default=None)` returns `default` for a missing or expired key, and removes an expired entry.
- `len(cache)` counts only live entries and `key in cache` is `True` only for a live key (both drop the expired entries they find).
- `delete(key)` returns `True` if a **live** entry was removed, else `False`.
- `ttl` must be positive, else `ValueError`.
