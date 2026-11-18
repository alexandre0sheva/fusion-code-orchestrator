def _check(m):
    if m and any(len(row) != len(m[0]) for row in m):
        raise ValueError("rows must all have the same length")


def identity(n):
    if n < 1:
        raise ValueError("n must be at least 1")
    return [[1 if i == j else 0 for j in range(n)] for i in range(n)]


def transpose(m):
    _check(m)
    if not m:
        return []
    return [[row[j] for row in m] for j in range(len(m[0]))]


def matmul(a, b):
    if not a or not b:
        raise ValueError("matrices must not be empty")
    _check(a)
    _check(b)
    n = len(a)
    return [[sum(a[i][k] * b[k][j] for k in range(n)) for j in range(n)] for i in range(n)]
