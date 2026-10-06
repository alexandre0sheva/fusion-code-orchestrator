`calc/` (a tokenizer in `tokenizer.py` and a parser in `parser.py`) evaluates arithmetic with `+ - * /`, parentheses and decimal numbers. `evaluate(text)` in `parser.py` is the public function and returns a `float`. Reported problems:

- `5-3` fails with a `ValueError` but `5 - 3` works.
- `10 - 4 - 3` gives 9 and `8 / 4 / 2` gives 4.
- `2 * -3`, `5 - -3` and `-(1 + 2)` fail.

Fix it so that:

- `+` and `-` also work as unary operators, anywhere an operand can start (`-3`, `--3`, `2 * -3`, `-(1+2)`). Unary operators bind tighter than `*` and `/`.
- `+ -` and `* /` are left-associative, and `* /` bind tighter than `+ -`.
- Spacing never matters.
- Division by zero raises `ZeroDivisionError`. Anything else malformed (empty input, unknown characters, unbalanced parentheses, a missing operand, two operands in a row) raises `ValueError`.
