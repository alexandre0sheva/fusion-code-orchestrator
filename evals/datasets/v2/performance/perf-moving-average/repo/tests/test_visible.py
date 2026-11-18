import unittest

from movavg import moving_average


class VisibleTests(unittest.TestCase):
    def test_window_two(self):
        self.assertEqual(moving_average([1, 2, 3, 4], 2), [1.5, 2.5, 3.5])

    def test_window_one(self):
        self.assertEqual(moving_average([4, 8], 1), [4.0, 8.0])
