Bug report for this repository (the `dacite` package): `is_instance()` ignores the item type of a
generic collection inside a `Union`.

`dacite.types.is_instance(["test"], Union[int, List[str]])` is `True`, as it should be, but so is
`is_instance(["test"], Union[int, List[int]])`: inside a union, `List[int]` is reduced to plain
`list`, so any list matches. Outside a union the item type is checked
(`is_instance(["test"], List[int])` is `False`). The same check must apply inside a union, and
unions of literals and `NewType`s must keep working.

Fix `is_instance`. The package's existing tests are in `tests/`.
