import unittest

from matrix import identity, matmul, transpose


class VisibleTests(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(identity(2), [[1, 0], [0, 1]])

    def test_transpose(self):
        self.assertEqual(transpose([[1, 2, 3], [4, 5, 6]]), [[1, 4], [2, 5], [3, 6]])

    def test_square_product(self):
        self.assertEqual(matmul([[1, 2], [3, 4]], [[5, 6], [7, 8]]), [[19, 22], [43, 50]])

    def test_matmul_by_identity(self):
        a = [[2, 3], [4, 5], [6, 7]]
        self.assertEqual(matmul(a, identity(2)), a)
