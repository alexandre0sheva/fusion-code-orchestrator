import unittest

from listutil import unique


class VisibleTests(unittest.TestCase):
    def test_removes_duplicates(self):
        self.assertEqual(sorted(unique([3, 1, 3, 2, 1])), [1, 2, 3])

    def test_empty(self):
        self.assertEqual(unique([]), [])

    def test_key_function(self):
        self.assertEqual(unique(["Apple", "avocado", "apple", "Banana", "banana"], key=str.lower),
                         ["Apple", "avocado", "Banana"])
