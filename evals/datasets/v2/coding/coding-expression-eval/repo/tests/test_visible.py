import unittest

from calc import evaluate


class VisibleTests(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(evaluate("2 + 3 * 4"), 14)

    def test_parentheses(self):
        self.assertEqual(evaluate("(2 + 3) * 4"), 20)

    def test_power_is_right_associative(self):
        self.assertEqual(evaluate("2 ** 3 ** 2"), 512)
        self.assertEqual(evaluate("2 ** 10"), 1024)
