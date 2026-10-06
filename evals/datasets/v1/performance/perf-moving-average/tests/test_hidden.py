import unittest

from movavg import moving_average


class HiddenTests(unittest.TestCase):
    def test_window_longer_than_list(self):
        self.assertEqual(moving_average([1, 2], 3), [])

    def test_window_equal_to_length(self):
        self.assertEqual(moving_average([2, 4, 6], 3), [4.0])

    def test_window_zero_is_an_error(self):
        with self.assertRaises(ValueError):
            moving_average([1, 2, 3], 0)

    def test_negative_window_is_an_error(self):
        with self.assertRaises(ValueError):
            moving_average([1, 2, 3], -2)

    def test_floats(self):
        result = moving_average([0.1, 0.2, 0.3, 0.4], 2)
        for got, want in zip(result, [0.15, 0.25, 0.35]):
            self.assertAlmostEqual(got, want)

    def test_empty_list(self):
        self.assertEqual(moving_average([], 2), [])
