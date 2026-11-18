Implement `parse_duration(text)` in `durations.py`: it returns the number of seconds as an `int`.

- `text` is one or more parts, each a non-negative integer followed by a unit: `d` (86400 s), `h` (3600), `m` (60) or `s` (1). Parts may be in any order and separated by spaces, and units are case-insensitive: `"1h30m15s"` is 5415, `"1H 30M"` is 5400.
- A bare integer such as `"90"` means seconds.
- Surrounding whitespace is ignored.
- Raise `ValueError` for an empty or blank string, an unknown unit (`"5x"`), a unit used twice (`"1h2h"`), a non-integer or negative number (`"1.5h"`, `"-5s"`), a number with no unit before the end of a longer text (`"5s 3"`), and any other stray text.
