import unittest

from roman import from_roman, to_roman


class RomanTests(unittest.TestCase):
    def test_known_values(self):
        cases = {1: "I", 4: "IV", 9: "IX", 14: "XIV", 40: "XL", 90: "XC", 400: "CD",
                 1994: "MCMXCIV", 3999: "MMMCMXCIX"}
        for number, text in cases.items():
            self.assertEqual(to_roman(number), text)

    def test_out_of_range(self):
        for bad in (0, -1, 4000):
            with self.assertRaises(ValueError):
                to_roman(bad)

    def test_wrong_type(self):
        for bad in (1.5, "3", True):
            with self.assertRaises(TypeError):
                to_roman(bad)

    def test_from_roman_is_case_insensitive(self):
        self.assertEqual(from_roman("mcmxciv"), 1994)

    def test_round_trip(self):
        for n in range(1, 4000):
            self.assertEqual(from_roman(to_roman(n)), n)

    def test_non_canonical_is_rejected(self):
        for bad in ("IIII", "VX", "IC", "IXI", "", "ABC", "MMMM"):
            with self.assertRaises(ValueError):
                from_roman(bad)
