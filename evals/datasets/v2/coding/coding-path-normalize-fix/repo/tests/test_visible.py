import unittest

from pathutil import normalize


class VisibleTests(unittest.TestCase):
    def test_collapses_dots_and_slashes(self):
        self.assertEqual(normalize("/a//b/./c"), "/a/b/c")

    def test_parent_segments(self):
        self.assertEqual(normalize("/a/b/../c"), "/a/c")

    def test_leading_dotdot_is_kept_on_relative_paths(self):
        cases = {"../a": "../a", "../../a": "../../a", "a/../../b": "../b", "..": "..", "../..": "../.."}
        for given, expected in cases.items():
            self.assertEqual(normalize(given), expected, given)
