Bug report for this repository (the `docstring_parser` package): the Google parser crashes on an
empty section.

Parsing this docstring raises an exception (an `AttributeError` on a missing regex match):

```
Short description

Example:

Raises:
    IOError: some error
```

It should parse, giving two meta entries: one for the empty `Example` section and one for the
`Raises` entry. Sections with content must parse as they do now.

Fix the Google parser. The package's existing tests are in `docstring_parser/tests/` (a copy of
the Google-style tests is in `tests/`).
