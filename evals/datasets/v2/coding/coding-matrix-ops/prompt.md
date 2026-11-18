Implement `identity`, `transpose` and `matmul` in `matrix.py`. A matrix is a list of rows, each a list of numbers.

- `identity(n)` is the n x n identity matrix; `n` must be at least 1, else `ValueError`.
- `transpose(m)` returns a new matrix whose rows are the columns of `m`, as lists. `transpose([])` is `[]`. A matrix whose rows differ in length raises `ValueError`.
- `matmul(a, b)` is the matrix product, a new matrix of lists. It raises `ValueError` when either matrix is empty or ragged, or when the number of columns of `a` differs from the number of rows of `b`.
- None of them may modify their arguments, and the result must not share row lists with an argument.
