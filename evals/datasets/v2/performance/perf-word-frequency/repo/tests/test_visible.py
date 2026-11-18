import unittest

from wordfreq import top_words


class VisibleTests(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(top_words("a b a c a b", 2), [("a", 3), ("b", 2)])

    def test_case_insensitive(self):
        self.assertEqual(top_words("Go go GO", 1), [("go", 3)])
