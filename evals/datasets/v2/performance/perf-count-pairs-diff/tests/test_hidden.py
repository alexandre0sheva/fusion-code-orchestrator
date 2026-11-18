import unittest

from pairs import count_pairs


class HiddenTests(unittest.TestCase):
    def test_order_matters(self):
        self.assertEqual(count_pairs([3, 1], 2), 0)

    def test_negative_difference(self):
        self.assertEqual(count_pairs([3, 1], -2), 1)

    def test_zero_difference_counts_equal_values_once_per_pair(self):
        self.assertEqual(count_pairs([4, 4, 4], 0), 3)

    def test_duplicates_multiply(self):
        self.assertEqual(count_pairs([1, 1, 3, 3], 2), 4)

    def test_empty_and_single(self):
        self.assertEqual(count_pairs([], 1), 0)
        self.assertEqual(count_pairs([7], 0), 0)

    def test_negative_values(self):
        self.assertEqual(count_pairs([-5, -2, 1, -2], 3), 3)
