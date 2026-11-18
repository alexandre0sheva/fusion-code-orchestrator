import unittest

from wordfreq import top_words


class HiddenTests(unittest.TestCase):
    def test_ties_are_alphabetical(self):
        self.assertEqual(top_words("b a b a c", 3), [("a", 2), ("b", 2), ("c", 1)])

    def test_k_larger_than_distinct_words(self):
        self.assertEqual(top_words("x y", 10), [("x", 1), ("y", 1)])

    def test_k_zero(self):
        self.assertEqual(top_words("x y", 0), [])

    def test_apostrophes_stay_in_words(self):
        self.assertEqual(top_words("don't stop don't", 1), [("don't", 2)])

    def test_empty_text(self):
        self.assertEqual(top_words("", 3), [])

    def test_punctuation_and_digits_separate_words(self):
        self.assertEqual(top_words("a, a; a1 a", 1), [("a", 4)])
