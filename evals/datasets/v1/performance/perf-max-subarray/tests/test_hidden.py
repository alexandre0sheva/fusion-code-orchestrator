import unittest

from maxsub import max_subarray_sum


class HiddenTests(unittest.TestCase):
    def test_all_negative_returns_the_largest_element(self):
        self.assertEqual(max_subarray_sum([-3, -1, -2]), -1)

    def test_single_element(self):
        self.assertEqual(max_subarray_sum([-7]), -7)

    def test_empty_is_an_error(self):
        with self.assertRaises(ValueError):
            max_subarray_sum([])

    def test_best_run_at_the_end(self):
        self.assertEqual(max_subarray_sum([-5, 1, 2, 3]), 6)

    def test_floats(self):
        self.assertAlmostEqual(max_subarray_sum([0.5, -0.25, 0.5]), 0.75)

    def test_zeros_and_negatives(self):
        self.assertEqual(max_subarray_sum([0, -1, 0]), 0)
