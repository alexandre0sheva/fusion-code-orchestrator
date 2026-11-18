Bug report for this repository (the `inflection` module): `titleize()` only capitalizes words
that start with a letter from a to z.

`inflection.titleize("ana índia")` returns "Ana índia": the second word starts with an accented
letter and is left in lower case. The expected result is "Ana Índia", and "Ana Índia" should
come back unchanged. Words that start with ASCII letters must keep the current behaviour.

Fix `titleize` so every word is capitalized, whatever letter it starts with. The existing tests
are in `tests/`.
