`parse_query(qs)` in `urlquery.py` parses a URL query string, but it crashes on a parameter without a value, loses all but the last value of a repeated key, and does not decode anything.

Fix it so that it returns a dict mapping each key to a **list** of its values:

- Keys appear in order of first appearance and each key's values in the order given: `a=1&b=2&a=3` is `{"a": ["1", "3"], "b": ["2"]}`.
- One leading `?` is ignored. Empty pairs (`a=1&&b=2`, a trailing `&`) are skipped. The empty string gives `{}`.
- A pair without `=` has the value `""` (`flag` is `{"flag": [""]}`). Only the **first** `=` splits a pair, so `a=b=c` gives `["b=c"]`.
- Keys and values are percent-decoded as UTF-8 and `+` means a space (`%2B` is a literal plus). An invalid escape such as `%zz` or a lone `%` is left as written.
