import unittest

from histogram import histogram


class HistogramTests(unittest.TestCase):
    def test_bins_are_half_open(self):
        self.assertEqual(histogram([0, 1, 2, 3, 4], 2, 0, 4), [2, 3])

    def test_the_maximum_lands_in_the_last_bin(self):
        self.assertEqual(histogram([0, 5, 10], 5), [1, 0, 1, 0, 1])
        self.assertEqual(histogram([1, 2, 3], 3), [1, 1, 1])

    def test_bin_edges(self):
        self.assertEqual(histogram([2, 4, 6, 8], 4, 0, 8), [0, 1, 1, 2])

    def test_values_outside_the_range_are_ignored(self):
        self.assertEqual(histogram([-5, 0, 5, 10, 15], 2, 0, 10), [1, 2])

    def test_empty_values(self):
        self.assertEqual(histogram([], 3), [0, 0, 0])
        self.assertEqual(histogram([], 2, 0, 10), [0, 0])

    def test_all_values_equal(self):
        self.assertEqual(histogram([5, 5, 5], 4), [3, 0, 0, 0])

    def test_degenerate_explicit_range(self):
        self.assertEqual(histogram([5, 5, 6], 3, 5, 5), [2, 0, 0])

    def test_single_bin_counts_everything_in_range(self):
        self.assertEqual(histogram([1, 2, 3, 4], 1), [4])

    def test_floats(self):
        self.assertEqual(histogram([0.5, 1.5, 2.5, 3.5], 2, 0, 4), [2, 2])

    def test_validation(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                histogram([1, 2], bad)
        with self.assertRaises(ValueError):
            histogram([1, 2], 2, 10, 0)

    def test_accepts_any_iterable(self):
        self.assertEqual(histogram(iter([1, 2, 3, 4]), 2), [2, 2])
