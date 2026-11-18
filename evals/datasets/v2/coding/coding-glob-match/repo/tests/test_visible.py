import unittest

from globbing import glob_match


class VisibleTests(unittest.TestCase):
    def test_star_stays_in_one_segment(self):
        self.assertTrue(glob_match("*.py", "main.py"))
        self.assertFalse(glob_match("*.py", "pkg/main.py"))

    def test_question_mark(self):
        self.assertTrue(glob_match("a?c", "abc"))
