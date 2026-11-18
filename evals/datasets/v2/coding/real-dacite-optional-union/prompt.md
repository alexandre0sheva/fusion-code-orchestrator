Bug report for this repository (the `dacite` package): `extract_optional()` drops members of an
optional union.

`dacite.types.extract_optional(Optional[Union[int, str]])` returns `int`, so a dataclass field
typed `Optional[Union[int, str]]` only accepts ints when `from_dict` builds it. The expected
result is `Union[int, str]`: every member except `None`. A plain `Optional[int]` must still give
`int`, and a type with no member other than `None` must still raise `ValueError`.

Fix `extract_optional`. The package's existing tests are in `tests/`.
