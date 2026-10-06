import unittest

from urlquery import parse_query


class VisibleTests(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(parse_query("a=1&b=2"), {"a": ["1"], "b": ["2"]})

    def test_empty(self):
        self.assertEqual(parse_query(""), {})

    def test_an_encoded_ampersand_stays_in_the_value(self):
        self.assertEqual(parse_query("a=1%262"), {"a": ["1&2"]})
