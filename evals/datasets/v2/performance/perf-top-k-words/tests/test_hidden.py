import unittest

from topwords import top_k_words


class HiddenTests(unittest.TestCase):
    def test_ties_are_alphabetical(self):
        self.assertEqual(top_k_words(["pear", "apple"], 2), ["apple", "pear"])

    def test_ties_after_a_leader(self):
        words = ["kiwi", "fig", "kiwi", "date", "fig", "kiwi", "date"]
        self.assertEqual(top_k_words(words, 3), ["kiwi", "date", "fig"])

    def test_k_larger_than_distinct(self):
        self.assertEqual(top_k_words(["a", "b", "a"], 10), ["a", "b"])

    def test_k_zero_or_negative(self):
        self.assertEqual(top_k_words(["a"], 0), [])
        self.assertEqual(top_k_words(["a"], -1), [])

    def test_empty_input(self):
        self.assertEqual(top_k_words([], 3), [])

    def test_case_is_significant(self):
        self.assertEqual(top_k_words(["Go", "go", "go"], 2), ["go", "Go"])
