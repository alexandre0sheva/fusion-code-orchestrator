import unittest

from intersect import intersect_sorted


class VisibleTests(unittest.TestCase):
    def test_common_values(self):
        self.assertEqual(intersect_sorted([1, 2, 4, 6], [2, 3, 4, 5]), [2, 4])

    def test_disjoint(self):
        self.assertEqual(intersect_sorted([1, 2], [3, 4]), [])
