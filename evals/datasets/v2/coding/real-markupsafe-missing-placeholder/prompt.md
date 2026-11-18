Bug report for this repository (the `markupsafe` package): `%` formatting of `Markup` silently
ignores an argument that has no placeholder.

With a plain string, `"<em></em>" % "foo"` raises `TypeError: not all arguments converted during
string formatting`. With `Markup`, `Markup("<em></em>") % "foo"` returns `Markup("<em></em>")`
and drops the argument, and so do `% 42` and `% ("foo", 42)` (the last should also raise).
`Markup` formatting should behave like `str` formatting, except that it escapes the arguments:
the existing behaviour for formats that do use their arguments, including mappings
(`Markup("%(x)s") % {"x": "<"}`), must not change.

Fix `Markup.__mod__`. The package's existing tests are in `tests/`.
