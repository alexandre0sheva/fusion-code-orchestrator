import unittest

from topwords import top_k_words


class VisibleTests(unittest.TestCase):
    def test_most_frequent_first(self):
        words = ["b", "a", "b", "c", "b", "a"]
        self.assertEqual(top_k_words(words, 2), ["b", "a"])

    def test_k_of_one(self):
        self.assertEqual(top_k_words(["x", "y", "y"], 1), ["y"])
