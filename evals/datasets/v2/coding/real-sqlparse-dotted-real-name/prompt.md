Bug report for this repository (the `sqlparse` package): `get_real_name()` is wrong for names with
more than two dotted parts.

For the identifier parsed from "warehouse.sales.orders.total" (the first token of
`sqlparse.parse(...)[0]`), `get_real_name()` returns "sales" instead of "total", and `get_name()`
follows it. With an alias, as in "p.q.r AS s", the real name should be "r" and the alias "s".
The parent name still anchors on the first dot (`get_parent_name()` of the first example is
"warehouse"), and two-part names and function calls must keep working as they do.

Fix how identifiers find their real name. The package's existing tests are in `tests/`.
