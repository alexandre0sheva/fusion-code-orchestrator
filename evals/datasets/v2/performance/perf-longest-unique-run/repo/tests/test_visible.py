import unittest

from runs import longest_unique_run


class VisibleTests(unittest.TestCase):
    def test_classic_example(self):
        self.assertEqual(longest_unique_run("abcabcbb"), "abc")

    def test_one_letter_repeated(self):
        self.assertEqual(longest_unique_run("bbbbb"), "b")
