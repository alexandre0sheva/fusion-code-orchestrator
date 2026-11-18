import unittest

from durations import parse_duration


class VisibleTests(unittest.TestCase):
    def test_compound(self):
        self.assertEqual(parse_duration("1h30m15s"), 5415)

    def test_days(self):
        self.assertEqual(parse_duration("2d"), 172800)

    def test_bare_integer_is_seconds(self):
        self.assertEqual(parse_duration("90"), 90)
        self.assertEqual(parse_duration(" 7 "), 7)
