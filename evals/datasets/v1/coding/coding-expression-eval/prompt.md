Implement `evaluate(text)` in `calc.py`: a small arithmetic evaluator that returns a `float`. Do not use `eval`, `exec`, `ast` or `compile`.

- Numbers are non-negative decimals such as `3`, `3.14` or `.5`. Whitespace between tokens is ignored.
- Operators: `+`, `-`, `*`, `/` and `**`, parentheses, and unary `+` and `-`.
- Precedence, loosest first: `+ -`; then `* /`; then unary `+ -`; then `**`. All binary operators except `**` are left-associative. `**` is right-associative, so `2 ** 3 ** 2` is 512. A unary minus on the left of `**` applies after it (`-2 ** 2` is -4), while one on the right belongs to the exponent (`2 ** -1` is 0.5).
- Division by zero raises `ZeroDivisionError`.
- Raise `ValueError` for empty or blank input, an unknown character, unbalanced parentheses, a missing operand (`1 +`, `* 2`), and two operands in a row (`2 3`).
