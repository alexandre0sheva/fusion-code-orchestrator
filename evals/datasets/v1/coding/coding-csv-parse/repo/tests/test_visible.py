import unittest

from csvparse import parse_csv


class VisibleTests(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(parse_csv("a,b\nc,d\n"), [["a", "b"], ["c", "d"]])

    def test_quoted_comma(self):
        self.assertEqual(parse_csv('"a,b",c'), [["a,b", "c"]])
