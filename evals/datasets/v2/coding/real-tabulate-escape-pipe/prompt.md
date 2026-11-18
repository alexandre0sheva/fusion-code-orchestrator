Bug report for this repository (the `tabulate` package): a `|` inside a cell breaks tables in
the `pipe` and `github` formats.

`tabulate([["foo|bar"]], headers=["spam|eggs"], tablefmt="pipe")` writes the pipe characters of
the cell contents as they are, so Markdown renderers split those cells into extra columns. In
these two formats a `|` inside a header or data cell must be escaped as `\|` (the column widths
then count the escaped text). Other formats, and cells without a pipe, must not change.

Fix the `pipe` and `github` formats. The package's existing tests are in `test/` (a copy of the
regression tests is in `tests/`).
