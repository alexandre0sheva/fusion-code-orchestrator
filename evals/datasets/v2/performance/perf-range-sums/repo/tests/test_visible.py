import unittest

from rangesums import range_sums


class VisibleTests(unittest.TestCase):
    def test_sums(self):
        self.assertEqual(range_sums([1, 2, 3, 4], [(0, 2), (1, 4)]), [3, 9])

    def test_no_queries(self):
        self.assertEqual(range_sums([1, 2], []), [])
