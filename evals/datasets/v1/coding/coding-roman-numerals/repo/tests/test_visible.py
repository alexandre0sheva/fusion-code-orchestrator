import unittest

from roman import from_roman, to_roman


class VisibleTests(unittest.TestCase):
    def test_to_roman(self):
        self.assertEqual(to_roman(4), "IV")
        self.assertEqual(to_roman(1994), "MCMXCIV")

    def test_from_roman(self):
        self.assertEqual(from_roman("XIV"), 14)

    def test_non_canonical_is_rejected(self):
        for bad in ("IIII", "VX", "IC", "IXI", "", "ABC", "MMMM"):
            with self.assertRaises(ValueError):
                from_roman(bad)
