import unittest

from pathutil import normalize


class NormalizeTests(unittest.TestCase):
    def test_absolute_paths(self):
        cases = {
            "/a//b/./c": "/a/b/c",
            "/a/b/../c": "/a/c",
            "/a/b/": "/a/b",
            "/": "/",
            "//": "/",
            "//a": "/a",
            "/a/..": "/",
            "/./": "/",
        }
        for given, expected in cases.items():
            self.assertEqual(normalize(given), expected, given)

    def test_dotdot_above_the_root_is_dropped(self):
        self.assertEqual(normalize("/../a"), "/a")
        self.assertEqual(normalize("/a/../../.."), "/")

    def test_relative_paths(self):
        cases = {
            "a/b": "a/b",
            "a//b/": "a/b",
            "./a": "a",
            "a/./b/../c": "a/c",
        }
        for given, expected in cases.items():
            self.assertEqual(normalize(given), expected, given)

    def test_leading_dotdot_is_kept_on_relative_paths(self):
        cases = {"../a": "../a", "../../a": "../../a", "a/../../b": "../b", "..": "..", "../..": "../.."}
        for given, expected in cases.items():
            self.assertEqual(normalize(given), expected, given)

    def test_nothing_left_is_dot(self):
        for given in ("", ".", "./", "a/..", "a/b/../..", "./."):
            self.assertEqual(normalize(given), ".", given)

    def test_names_that_merely_contain_dots(self):
        self.assertEqual(normalize("/a/..b/c.d/..."), "/a/..b/c.d/...")
