Bug report for this repository (the `dacite` package): a `KeyError` raised inside a type hook is
swallowed.

With `from_dict(X, {"i": 1}, config=Config(type_hooks={int: hook}))`, if `hook` raises
`KeyError`, `from_dict` does not let it through: it treats the error as if the field were
missing from the data and goes on (using a default, or reporting a missing value). Errors raised
by user hooks must propagate unchanged; a field that really is absent from the data must keep
its current handling (default value or `MissingValueError`).

Fix `from_dict`. The package's existing tests are in `tests/`.
