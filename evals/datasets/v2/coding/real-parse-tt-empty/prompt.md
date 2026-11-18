Bug report for this repository (the `parse` package): the `tt` (time) type matches an empty
string.

`parse.parse("{:tt}", "")` returns a result holding `time(0, 0)` instead of `None`, and so does
`parse.parse("Meet at {:tt}", "Meet at ")`. `parse.search("{:tt}", "there is no time here")`
reports a match at position 0, and `parse.findall("{:tt}", "no time")` never stops yielding
empty matches. Real times such as "10:21:36 PM" or "10:21" must keep parsing as they do now.

Fix the `tt` type so that a field of that type needs an actual time. The package's existing
tests are in `tests/`.
