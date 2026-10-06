import unittest

from words import top_words


class VisibleTests(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(top_words("a b a c a b", 2), [("a", 3), ("b", 2)])

    def test_zero(self):
        self.assertEqual(top_words("a b", 0), [])

    def test_apostrophes_inside_words(self):
        self.assertEqual(top_words("don't don't can't", 2), [("don't", 2), ("can't", 1)])
