import unittest

from twosum import two_sum


class HiddenTests(unittest.TestCase):
    def test_uses_the_smallest_earlier_index(self):
        self.assertEqual(two_sum([1, 1, 5], 6), (0, 2))

    def test_first_j_wins(self):
        self.assertEqual(two_sum([3, 2, 4], 6), (1, 2))

    def test_same_element_is_not_used_twice(self):
        self.assertIsNone(two_sum([3], 6))
        self.assertIsNone(two_sum([3, 4], 6))

    def test_zero_target_with_zeros(self):
        self.assertEqual(two_sum([0, 0], 0), (0, 1))

    def test_negative_numbers(self):
        self.assertEqual(two_sum([-4, 9, 4, 1], 0), (0, 2))

    def test_empty(self):
        self.assertIsNone(two_sum([], 0))
