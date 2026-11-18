Bug report for this repository (the `humanize` package): `fractional()` produces degenerate
output when the fractional part rounds to a whole number.

`humanize.fractional(0)` returns "0/1" instead of "0", and a value whose fractional part rounds
up to one, such as `fractional(2.9999999)`, returns "2 1/1" instead of "3" (likewise
`fractional(0.9999999)` should be "1" and `fractional(-2.9999999)` should be "-3"). Integers
and values like `1.0000` that are already handled must keep their output.

Fix `fractional` so a fraction that reduces to a whole number is shown as that whole number. The
package's existing tests are in `tests/`.
