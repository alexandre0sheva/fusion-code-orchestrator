Bug report for this repository (the `inflection` module): "passerby" is pluralized as
"passerbys".

`inflection.pluralize("passerby")` returns "passerbys"; the plural is "passersby", and
`inflection.singularize("passersby")` should give "passerby" back. Pluralizing a word that is
already plural ("passersby") must leave it as it is, and the other rules must not change.

Fix the inflection rules for this word. The existing tests are in `tests/`.
