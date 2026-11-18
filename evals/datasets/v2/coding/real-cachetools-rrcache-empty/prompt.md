Bug report for this repository (the `cachetools` package): `RRCache.popitem()` on an empty cache
raises the wrong exception when the cache uses a custom `choice` function.

The documented contract of `RRCache(maxsize, choice=...)` only requires `choice` to pick from a
non-empty sequence, and `popitem()` on an empty cache must raise `KeyError` like every other
cache class. With the default `random.choice` it does, but with any other valid choice function
it does not: for example `RRCache(maxsize=2, choice=min).popitem()` raises `ValueError` from
`min()` instead of `KeyError`, which also breaks eviction code that relies on `KeyError`.

Fix `RRCache` so an empty cache is detected before `choice` is called. The package's existing
tests are in `tests/`.
