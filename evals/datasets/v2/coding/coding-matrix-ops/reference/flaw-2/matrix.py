def _check(m):
    if m and any(len(row) != len(m[0]) for row in m):
        raise ValueError("rows must all have the same length")


def identity(n):
    if n < 1:
        raise ValueError("n must be at least 1")
    return [[1 if i == j else 0 for j in range(n)] for i in range(n)]


def transpose(m):
    return list(zip(*m))


def matmul(a, b):
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
