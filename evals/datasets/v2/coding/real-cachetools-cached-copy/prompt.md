Bug report for this repository (the `cachetools` package): `copy.copy()` and `copy.deepcopy()`
fail on a `@cached` wrapper of a callable that has no `__qualname__`.

Wrapping, for instance, a `functools.partial` with `cachetools.cached(LRUCache(maxsize=2))`
works for calls, but `copy.copy(func)` and `copy.deepcopy(func)` raise an `AttributeError`,
because copying falls back to `__reduce__`, which returns the missing `__qualname__`. Like
`functools.lru_cache`, a cached wrapper should be copied by reference: both copies should return
the very same wrapper object, for any wrapped callable.

Fix the `@cached` wrapper so copying works for every wrapped callable. The package's existing
tests are in `tests/`.
