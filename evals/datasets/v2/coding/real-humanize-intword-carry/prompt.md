Bug report for this repository (the `humanize` package): `intword()` does not carry to the next
unit for very large numbers.

`humanize.intword(10**6 - 1)` correctly gives "1.0 million": after rounding the mantissa reaches
the next power, so the function moves up a unit. For numbers above about 10**22 that carry no
longer happens: `humanize.intword(10**24 - 1)` returns "1000.0 sextillion" instead of
"1.0 septillion", and the same goes for the other very large powers (with the default format and
with a custom one such as "%.2f").

Fix `intword` so the carry works at every magnitude, without changing its output for numbers
that are already handled correctly. The package's existing tests are in `tests/`.
