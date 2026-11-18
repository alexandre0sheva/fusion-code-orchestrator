Bug report for this repository (the `cachetools` package): assigning an already-expired value
to an existing key of a `TLRUCache` is silently ignored and the old value stays.

`TLRUCache(maxsize, ttu)` computes each item's expiry with `ttu(key, value, now)`, and an item
whose expiry is not in the future is not stored. When the key already holds a value, though,
the assignment returns early and leaves the previous entry in place: after `cache[1] = 5`
followed by `cache[1] = 0` with `ttu = lambda k, v, t: t + v`, `cache[1]` still returns 5.
That breaks the mapping contract that `cache[key] = value` either stores `value` or leaves the
key absent.

Fix `TLRUCache` so that assigning an already-expired value removes the key (and keeps `len`,
`currsize` and iteration consistent). The package's existing tests are in `tests/`.
