Implement `glob_match(pattern, name)` in `globbing.py`. It returns `True` when the whole of `name` matches `pattern`. Matching is case-sensitive.

- `*` matches any run of characters (possibly none) **except `/`**. `**` matches any run of characters including `/`.
- `?` matches exactly one character other than `/`.
- `[abc]` matches one of the listed characters, `[a-c]` one in a range, `[!abc]` one that is not listed. A bracket class never matches `/`, even a negated one.
- A backslash makes the next character literal: `\*` matches only `*`.
- Anything else matches itself.
- Raise `ValueError` for an unterminated `[`, an empty class (`[]` or `[!]`), a backwards range (`[z-a]`), or a pattern ending in a lone backslash.
