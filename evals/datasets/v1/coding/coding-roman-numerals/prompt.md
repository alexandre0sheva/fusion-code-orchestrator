Implement `to_roman(n)` and `from_roman(text)` in `roman.py`.

- `to_roman(n)` converts an integer from 1 to 3999 to its canonical Roman numeral (`4` is `IV`, `1994` is `MCMXCIV`). It raises `ValueError` for any integer outside that range and `TypeError` for anything that is not an `int` (a `bool` is not accepted either).
- `from_roman(text)` is the inverse. It accepts upper or lower case and returns the integer. It accepts **only canonical numerals**: `IIII`, `VX`, `IC`, `IXI` and the empty string raise `ValueError`.
- Round trips hold: `from_roman(to_roman(n)) == n` for every n in 1..3999.
