Bug report for this repository (the `sqlparse` package): function grouping depends on the case of
`AS` in `CREATE TABLE ... AS SELECT`.

`sqlparse.parse("CREATE TABLE tbl1 AS SELECT coalesce(t1.col1, 0) AS col1 FROM t1")` groups
`coalesce(...)` as a function with alias `col1`, but the same statement written in lower case
(`create table tbl1 as select coalesce(t1.col1, 0) as col1 from t1`) does not: the function call
is not grouped and the alias is lost. SQL keywords are case-insensitive, so both must parse the
same way.

Fix the grouping. The package's existing tests are in `tests/`.
