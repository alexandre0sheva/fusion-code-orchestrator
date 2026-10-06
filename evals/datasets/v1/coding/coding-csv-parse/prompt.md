Implement `parse_csv(text)` in `csvparse.py`; it returns a list of records, each a list of strings. Do not use the `csv` module.

- Fields are separated by `,`. Records end at `\n` or `\r\n`. A single newline at the very end of the text does not start another record. `""` (empty text) returns `[]`.
- A field that starts with `"` is quoted: inside it commas and newlines are ordinary characters, and `""` stands for one `"`. The closing quote must be followed by `,`, a line end or the end of the text.
- A `"` in the middle of an unquoted field is an ordinary character: `ab"c` stays `ab"c`.
- Empty fields are kept: `a,,b` is `["a", "", "b"]`. A blank line between records is a record with one empty field, `[""]`.
- Fields are not trimmed.
- An unterminated quoted field, or a closing quote followed by anything else, raises `ValueError`.
