import unittest

from unique import unique


class VisibleTests(unittest.TestCase):
    def test_removes_repeats(self):
        self.assertEqual(unique([1, 2, 2, 3]), [1, 2, 3])

    def test_empty(self):
        self.assertEqual(unique([]), [])
