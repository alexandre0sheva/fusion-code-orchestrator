import unittest

from twosum import two_sum


class VisibleTests(unittest.TestCase):
    def test_finds_pair(self):
        self.assertEqual(two_sum([2, 7, 11, 15], 9), (0, 1))

    def test_no_pair(self):
        self.assertIsNone(two_sum([1, 2, 3], 10))
