def _check(m: list[list[float]]) -> None:
    if m and any(len(row) != len(m[0]) for row in m):
        raise ValueError("rows must all have the same length")


def identity(n: int) -> list[list[int]]:
    """The n x n identity matrix."""
    if n < 1:
        raise ValueError("n must be at least 1")
    return [[1 if i == j else 0 for j in range(n)] for i in range(n)]


def transpose(m: list[list[float]]) -> list[list[float]]:
    """Rows become columns."""
    _check(m)
    if not m:
        return []
    return [[row[j] for row in m] for j in range(len(m[0]))]


def matmul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    """The matrix product of ``a`` and ``b``."""
    if not a or not b:
        raise ValueError("matrices must not be empty")
    _check(a)
    _check(b)
    if len(a[0]) != len(b):
        raise ValueError("columns of a must equal rows of b")
    return [
        [sum(a[i][k] * b[k][j] for k in range(len(b))) for j in range(len(b[0]))]
        for i in range(len(a))
    ]
