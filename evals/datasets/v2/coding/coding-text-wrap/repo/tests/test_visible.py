import unittest

from wrapping import wrap


class VisibleTests(unittest.TestCase):
    def test_greedy_fill(self):
        self.assertEqual(wrap("the quick brown fox", 10), ["the quick", "brown fox"])

    def test_paragraphs(self):
        self.assertEqual(wrap("one\n\ntwo", 10), ["one", "", "two"])

    def test_last_chunk_can_be_joined_by_the_next_word(self):
        self.assertEqual(wrap("abcdefg hi", 5), ["abcde", "fg hi"])
