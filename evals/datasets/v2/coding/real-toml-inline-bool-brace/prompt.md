Bug report for this repository (the `toml` package): an inline table whose last value is a
boolean followed by " }" fails to load.

A dependency table like the ones in real `pyproject.toml` files raises an error instead of
loading:

```toml
[deps]
tool = { version = "^2.1", optional = true }
```

It should load as `{"deps": {"tool": {"version": "^2.1", "optional": True}}}`, and the same goes
for `false` in that position. Inline tables that end in other kinds of value already work.

Fix the decoder. The package's existing tests are in `tests/`.
