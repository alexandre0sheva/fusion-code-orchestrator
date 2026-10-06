import unittest

from calc.parser import evaluate


class VisibleTests(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(evaluate("2 + 3 * 4"), 14)

    def test_spaced_subtraction(self):
        self.assertEqual(evaluate("5 - 3"), 2)

    def test_division_is_left_associative(self):
        self.assertEqual(evaluate("8 / 4 / 2"), 1)
        self.assertEqual(evaluate("100 / 10 * 5"), 50)
