import unittest

from durations import parse_duration


class ParseDurationTests(unittest.TestCase):
    def test_compound(self):
        self.assertEqual(parse_duration("1h30m15s"), 5415)

    def test_any_order_and_spaces_and_case(self):
        self.assertEqual(parse_duration("30m1h"), 5400)
        self.assertEqual(parse_duration("1H 30M"), 5400)

    def test_bare_integer_is_seconds(self):
        self.assertEqual(parse_duration("90"), 90)
        self.assertEqual(parse_duration(" 7 "), 7)

    def test_days_and_zero(self):
        self.assertEqual(parse_duration("2d"), 172800)
        self.assertEqual(parse_duration("0s"), 0)

    def test_empty_is_an_error(self):
        for text in ("", "   "):
            with self.assertRaises(ValueError):
                parse_duration(text)

    def test_unknown_unit_is_an_error(self):
        with self.assertRaises(ValueError):
            parse_duration("5x")

    def test_repeated_unit_is_an_error(self):
        with self.assertRaises(ValueError):
            parse_duration("1h2h")

    def test_non_integer_and_negative_are_errors(self):
        for text in ("1.5h", "-5s"):
            with self.assertRaises(ValueError):
                parse_duration(text)

    def test_stray_text_is_an_error(self):
        for text in ("5s!", "5s 3", "h", "abc"):
            with self.assertRaises(ValueError):
                parse_duration(text)
