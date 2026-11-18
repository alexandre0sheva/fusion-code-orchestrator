import unittest

from maxsub import max_subarray_sum


class VisibleTests(unittest.TestCase):
    def test_mixed(self):
        self.assertEqual(max_subarray_sum([-2, 1, -3, 4, -1, 2, 1, -5, 4]), 6)

    def test_all_positive(self):
        self.assertEqual(max_subarray_sum([1, 2, 3]), 6)
