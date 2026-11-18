Bug report for this repository (the `sqlparse` package): `sqlparse.split()` treats
`BEGIN TRANSACTION` as the start of a `BEGIN ... END` block.

This script holds four statements:

```sql
BEGIN TRANSACTION;
UPDATE accounts SET balance = balance - 10 WHERE id = 1;
UPDATE accounts SET balance = balance + 10 WHERE id = 2;
END TRANSACTION;
```

but `split()` returns them as one, because `BEGIN` opens a block that only `END` closes. A
`BEGIN` followed by a transaction keyword (`TRANSACTION`, `WORK`, `TRAN`, `DEFERRED`,
`IMMEDIATE`, `EXCLUSIVE`, ...) starts a transaction statement, not a block. Real `BEGIN ... END`
blocks (for example in procedures and triggers) must keep splitting as one statement.

Fix the statement splitter. The package's existing tests are in `tests/`.
