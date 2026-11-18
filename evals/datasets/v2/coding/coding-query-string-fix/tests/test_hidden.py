import unittest

from urlquery import parse_query


class ParseQueryTests(unittest.TestCase):
    def test_repeated_keys_collect_values_in_order(self):
        result = parse_query("a=1&b=2&a=3")
        self.assertEqual(result, {"a": ["1", "3"], "b": ["2"]})
        self.assertEqual(list(result), ["a", "b"])

    def test_leading_question_mark(self):
        self.assertEqual(parse_query("?x=1"), {"x": ["1"]})

    def test_empty_pairs_are_skipped(self):
        self.assertEqual(parse_query("a=1&&b=2&"), {"a": ["1"], "b": ["2"]})
        self.assertEqual(parse_query("?"), {})
        self.assertEqual(parse_query("&&"), {})

    def test_parameter_without_a_value(self):
        self.assertEqual(parse_query("flag&a=1"), {"flag": [""], "a": ["1"]})
        self.assertEqual(parse_query("empty="), {"empty": [""]})

    def test_only_the_first_equals_splits(self):
        self.assertEqual(parse_query("a=b=c"), {"a": ["b=c"]})

    def test_percent_decoding_and_plus(self):
        self.assertEqual(parse_query("q=hello%20world"), {"q": ["hello world"]})
        self.assertEqual(parse_query("q=hello+world"), {"q": ["hello world"]})
        self.assertEqual(parse_query("q=1%2B1"), {"q": ["1+1"]})

    def test_utf8_and_keys_are_decoded(self):
        self.assertEqual(parse_query("caf%C3%A9=%E2%82%AC"), {"café": ["€"]})
        self.assertEqual(parse_query("a%20b=1"), {"a b": ["1"]})

    def test_invalid_escapes_are_left_alone(self):
        self.assertEqual(parse_query("a=%zz&b=100%"), {"a": ["%zz"], "b": ["100%"]})

    def test_an_encoded_ampersand_stays_in_the_value(self):
        self.assertEqual(parse_query("a=1%262"), {"a": ["1&2"]})
