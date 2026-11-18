Bug report for this repository (the `toolz` package): `interpose()` fails on an empty sequence.

`list(toolz.interpose("a", [1, 2, 3]))` gives `[1, "a", 2, "a", 3]` as documented, but
`toolz.interpose("a", [])` raises `StopIteration` (inside a generator this becomes a
`RuntimeError`) instead of returning an empty iterator. `list(interpose("a", []))` should be
`[]`.

Fix `interpose`. The package's existing tests are in `toolz/tests/` (a copy of the itertoolz
tests is in `tests/`).
