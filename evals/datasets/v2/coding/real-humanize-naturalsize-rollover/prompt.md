Bug report for this repository (the `humanize` package): `naturalsize()` can print a mantissa
equal to the base instead of moving to the next unit.

`humanize.naturalsize(999999)` returns "1000.0 kB" where "1.0 MB" is expected: the suffix is
picked from the unrounded size, and the format then rounds 999.999 up to 1000.0. The same happens
at the other unit boundaries (`999999999` should be "1.0 GB"), in binary mode
(`naturalsize(1024**2 - 1, binary=True)` should be "1.0 MiB") and in GNU mode
(`naturalsize(1024**2 - 1, gnu=True)` should be "1.0M").

Fix `naturalsize` so the result never shows a mantissa that rounds to the base while a larger
unit is available. The package's existing tests are in `tests/`.
