import unittest

from csvparse import parse_csv


class ParseCsvTests(unittest.TestCase):
    def test_simple_records(self):
        self.assertEqual(parse_csv("a,b\nc,d"), [["a", "b"], ["c", "d"]])

    def test_trailing_newline_adds_no_record(self):
        self.assertEqual(parse_csv("a,b\n"), [["a", "b"]])
        self.assertEqual(parse_csv("a,b\r\nc,d\r\n"), [["a", "b"], ["c", "d"]])

    def test_empty_text(self):
        self.assertEqual(parse_csv(""), [])

    def test_empty_fields(self):
        self.assertEqual(parse_csv("a,,b"), [["a", "", "b"]])
        self.assertEqual(parse_csv(",\n"), [["", ""]])

    def test_quoted_commas_and_newlines(self):
        self.assertEqual(parse_csv('"a,b","c\nd"\ne,f'), [["a,b", "c\nd"], ["e", "f"]])

    def test_escaped_quote(self):
        self.assertEqual(parse_csv('"say ""hi""",x'), [['say "hi"', "x"]])

    def test_quoted_empty_field(self):
        self.assertEqual(parse_csv('"",x'), [["", "x"]])

    def test_quote_inside_unquoted_field_is_literal(self):
        self.assertEqual(parse_csv('ab"c,d'), [['ab"c', "d"]])

    def test_blank_line_is_a_record(self):
        self.assertEqual(parse_csv("a\n\nb"), [["a"], [""], ["b"]])

    def test_fields_are_not_trimmed(self):
        self.assertEqual(parse_csv(" a , b "), [[" a ", " b "]])

    def test_unterminated_quote(self):
        with self.assertRaises(ValueError):
            parse_csv('"abc')

    def test_text_after_closing_quote(self):
        with self.assertRaises(ValueError):
            parse_csv('"abc"x,y')
