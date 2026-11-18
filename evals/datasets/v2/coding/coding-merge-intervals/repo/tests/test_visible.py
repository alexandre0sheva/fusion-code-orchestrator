import unittest

from intervals import merge_intervals


class VisibleTests(unittest.TestCase):
    def test_example(self):
        self.assertEqual(merge_intervals([(1, 3), (2, 6), (8, 10), (6, 7)]), [(1, 7), (8, 10)])

    def test_empty(self):
        self.assertEqual(merge_intervals([]), [])
