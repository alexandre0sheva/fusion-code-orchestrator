import unittest

from runs import longest_unique_run


class HiddenTests(unittest.TestCase):
    def test_window_never_moves_back(self):
        self.assertEqual(longest_unique_run("abba"), "ab")

    def test_run_in_the_middle(self):
        self.assertEqual(longest_unique_run("pwwkew"), "wke")

    def test_first_of_equal_runs(self):
        self.assertEqual(longest_unique_run("abcdabc"), "abcd")

    def test_repeat_inside_the_window(self):
        self.assertEqual(longest_unique_run("dvdf"), "vdf")

    def test_empty_string(self):
        self.assertEqual(longest_unique_run(""), "")

    def test_spaces_and_unicode_count(self):
        self.assertEqual(longest_unique_run("née née"), "née ")
