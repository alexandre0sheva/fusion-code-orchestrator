Bug report for this repository (the `parse` package): the `%z` directive of strftime-style
fields accepts offsets that `strptime` rejects, and rejects one it accepts.

`parse.parse("{:%H:%M%z}", "10:30|0200")` and `parse.parse("{:%H:%M%z}", "10:30+02")` match the
pattern and then crash inside `strptime` with `ValueError`; they should return `None` (and so
should a lowercase "10:30z"), as should the same values found with `search()`. Meanwhile
`parse.parse("{:%Y-%m-%d %H:%M%z}", "2024-01-02 10:30Z")` returns `None`, although `strptime`'s
`%z` accepts "Z" as UTC: it should give the same `datetime` as `strptime`, with a zero UTC
offset. Offsets such as "+0200", "+02:00" and "-0530" must keep working.

Fix the `%z` pattern. The package's existing tests are in `tests/`.
