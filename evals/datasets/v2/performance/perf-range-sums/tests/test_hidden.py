import unittest

from rangesums import range_sums


class HiddenTests(unittest.TestCase):
    def test_end_is_exclusive(self):
        self.assertEqual(range_sums([5, 6, 7], [(1, 2)]), [6])

    def test_end_past_the_list_is_clamped(self):
        self.assertEqual(range_sums([1, 2, 3], [(1, 10)]), [5])

    def test_negative_start_is_clamped(self):
        self.assertEqual(range_sums([1, 2, 3], [(-5, 2)]), [3])

    def test_reversed_or_empty_range_is_zero(self):
        self.assertEqual(range_sums([1, 2, 3], [(2, 1), (1, 1)]), [0, 0])

    def test_empty_values(self):
        self.assertEqual(range_sums([], [(0, 3)]), [0])

    def test_floats(self):
        self.assertAlmostEqual(range_sums([0.5, 0.25, 0.25], [(0, 3)])[0], 1.0)
