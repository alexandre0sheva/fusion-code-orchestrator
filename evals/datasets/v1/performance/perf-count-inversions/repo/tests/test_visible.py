import unittest

from inversions import count_inversions


class VisibleTests(unittest.TestCase):
    def test_sorted_has_none(self):
        self.assertEqual(count_inversions([1, 2, 3, 4]), 0)

    def test_reversed_has_all(self):
        self.assertEqual(count_inversions([4, 3, 2, 1]), 6)
