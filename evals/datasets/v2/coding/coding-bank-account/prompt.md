Implement `Account`, `InsufficientFunds` and `transfer` in `bank.py`. Money is exact: use `decimal.Decimal`.

- An amount may be an `int`, a `str` such as `"10.50"` or a `Decimal`. A `float` or a `bool` raises `TypeError`. It must be greater than zero and have at most two decimal places, else `ValueError`.
- `Account(owner, balance="0", overdraft="0")`: `balance` and `overdraft` follow the amount rules except that they may be zero. `account.balance` is a `Decimal`; `account.owner` the owner; `account.history` a list.
- `deposit(amount)` and `withdraw(amount)` return the new balance. A withdrawal may take the balance down to `-overdraft` but no further; otherwise it raises `InsufficientFunds` and changes nothing.
- Every successful operation appends a tuple `(kind, amount, balance_after)` to `history`, with `kind` one of `"deposit"`, `"withdraw"`, `"transfer_out"`, `"transfer_in"`, and `amount` a `Decimal`.
- `transfer(src, dst, amount)` moves money atomically. It raises `ValueError` if `src is dst`. If `src` cannot cover it, it raises `InsufficientFunds` and **neither account changes** (no history either). On success `src` has a `"transfer_out"` entry and `dst` a `"transfer_in"` entry.
