Bug report for this repository (the `parse` package): a float field with precision 0 does not
parse what Python formats with that precision.

`format(12.0, ".0f")` is "12", without a decimal point, yet `parse.parse("a {:.0f} b", "a 12 b")`
returns `None`; the same goes for a width, as in `parse("foo_{:02.0f}t", "foo_20t")`, and for the
`F` (Decimal) type. Parsing should round-trip with formatting: such fields should accept "12"
(as 12.0) and, since ".0f" never writes a decimal point, should no longer accept "12.0".

Fix the `f` and `F` types for precision 0. The package's existing tests are in `tests/`.
