Bug report for this repository (the `humanize` package): `metric()` shows one significant digit
too many when rounding carries the mantissa to the next power of ten.

With the default precision of 3, `humanize.metric(9999)` returns "10.00 k" (four significant
digits) where "10.0 k" is expected; `metric(99999)` gives "100.0 k" instead of "100 k", and
`metric(9.99999)` gives "10.00" instead of "10.0". Negative values behave the same, and so do
other precisions (`metric(9.999, "", 2)` should be "10"). Values that do not carry, such as
`metric(999.4)` → "999", are already right and must stay so.

Fix `metric` so the number of significant digits is always `precision`, including after a
rounding carry. The package's existing tests are in `tests/`.
