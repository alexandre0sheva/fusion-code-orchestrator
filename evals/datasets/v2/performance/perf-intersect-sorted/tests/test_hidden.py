import unittest

from intersect import intersect_sorted


class HiddenTests(unittest.TestCase):
    def test_each_value_once(self):
        self.assertEqual(intersect_sorted([1, 1, 2, 2], [1, 1, 2]), [1, 2])

    def test_one_side_empty(self):
        self.assertEqual(intersect_sorted([], [1, 2]), [])
        self.assertEqual(intersect_sorted([1, 2], []), [])

    def test_identical_lists(self):
        self.assertEqual(intersect_sorted([1, 2, 3], [1, 2, 3]), [1, 2, 3])

    def test_strings(self):
        self.assertEqual(intersect_sorted(["a", "b", "d"], ["b", "c", "d"]), ["b", "d"])

    def test_negative_numbers(self):
        self.assertEqual(intersect_sorted([-3, -1, 0], [-1, 0, 5]), [-1, 0])

    def test_does_not_modify_the_inputs(self):
        a, b = [1, 2, 2], [2, 2, 3]
        intersect_sorted(a, b)
        self.assertEqual((a, b), ([1, 2, 2], [2, 2, 3]))
