Bug report for this repository (the `funcy` package): `once_per()` and `once_per_args` fail when
an argument is unhashable.

A function decorated with `@once_per("n")` or `@once_per_args` should run once per distinct
value of its arguments, unhashable ones included. Calling it with a list, a dict, a set or a
tuple holding a list raises `TypeError: unhashable type` instead. After the fix, calling it with
such a value (positionally or by keyword) runs it the first time and returns `None` for a repeat
of an equal value, exactly as for hashable values.

Fix `once_per`. The package's existing tests are in `tests/`.
