import unittest

from histogram import histogram


class VisibleTests(unittest.TestCase):
    def test_even_split(self):
        self.assertEqual(histogram([0, 1, 2, 3], 2, 0, 4), [2, 2])

    def test_bins_must_be_positive(self):
        with self.assertRaises(ValueError):
            histogram([1], 0)

    def test_all_values_equal(self):
        self.assertEqual(histogram([5, 5, 5], 4), [3, 0, 0, 0])
