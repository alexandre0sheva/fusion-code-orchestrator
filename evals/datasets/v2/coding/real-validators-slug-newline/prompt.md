Bug report for this repository (the `validators` package): `slug()` accepts a value that ends in
a newline.

`validators.slug("hello-world\n")` returns `True`, although a slug may only contain lower-case
letters, digits and hyphens. A trailing newline
must make the value invalid (the function then returns a `ValidationError`, which is falsy);
every value that is valid today must stay valid.

Fix `slug`. The package's existing tests are in `tests/`.
