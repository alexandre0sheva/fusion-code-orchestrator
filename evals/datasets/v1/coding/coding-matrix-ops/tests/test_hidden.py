import unittest

from matrix import identity, matmul, transpose


class MatrixTests(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(identity(3), [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
        with self.assertRaises(ValueError):
            identity(0)

    def test_transpose_rectangular(self):
        self.assertEqual(transpose([[1, 2, 3], [4, 5, 6]]), [[1, 4], [2, 5], [3, 6]])

    def test_transpose_returns_lists_and_empty(self):
        result = transpose([[1, 2]])
        self.assertEqual(result, [[1], [2]])
        self.assertTrue(all(isinstance(row, list) for row in result))
        self.assertEqual(transpose([]), [])

    def test_transpose_ragged(self):
        with self.assertRaises(ValueError):
            transpose([[1, 2], [3]])

    def test_matmul_rectangular(self):
        a = [[1, 2, 3], [4, 5, 6]]
        b = [[7, 8], [9, 10], [11, 12]]
        self.assertEqual(matmul(a, b), [[58, 64], [139, 154]])

    def test_matmul_by_identity(self):
        a = [[2, 3], [4, 5], [6, 7]]
        self.assertEqual(matmul(a, identity(2)), a)

    def test_matmul_dimension_mismatch(self):
        with self.assertRaises(ValueError):
            matmul([[1, 2]], [[1, 2]])

    def test_matmul_empty_or_ragged(self):
        for a, b in (([], [[1]]), ([[1]], []), ([[1, 2], [3]], [[1], [2]])):
            with self.assertRaises(ValueError):
                matmul(a, b)

    def test_arguments_are_untouched_and_not_shared(self):
        a = [[1, 2], [3, 4]]
        b = [[1, 0], [0, 1]]
        result = matmul(a, b)
        self.assertEqual(a, [[1, 2], [3, 4]])
        result[0][0] = 99
        self.assertEqual(a[0][0], 1)
        t = transpose(a)
        t[0][0] = 77
        self.assertEqual(a[0][0], 1)
