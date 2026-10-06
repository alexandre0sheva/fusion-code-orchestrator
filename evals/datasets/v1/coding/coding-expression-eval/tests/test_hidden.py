import unittest

from calc import evaluate


class EvaluateTests(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(evaluate("2 + 3 * 4"), 14)
        self.assertEqual(evaluate("2 * 3 + 4"), 10)
        self.assertEqual(evaluate("(2 + 3) * 4"), 20)

    def test_left_associativity(self):
        self.assertEqual(evaluate("10 - 4 - 3"), 3)
        self.assertEqual(evaluate("64 / 4 / 2"), 8)

    def test_decimals_and_whitespace(self):
        self.assertAlmostEqual(evaluate("  1.5 * 2 + .5 "), 3.5)

    def test_returns_a_float(self):
        self.assertIsInstance(evaluate("4 / 2"), float)
        self.assertEqual(evaluate("7 / 2"), 3.5)

    def test_unary_operators(self):
        self.assertEqual(evaluate("-3 + 5"), 2)
        self.assertEqual(evaluate("--3"), 3)
        self.assertEqual(evaluate("2 * -3"), -6)
        self.assertEqual(evaluate("+4"), 4)
        self.assertEqual(evaluate("-(2 + 3)"), -5)

    def test_power_is_right_associative(self):
        self.assertEqual(evaluate("2 ** 3 ** 2"), 512)
        self.assertEqual(evaluate("2 ** 10"), 1024)

    def test_unary_minus_and_power(self):
        self.assertEqual(evaluate("-2 ** 2"), -4)
        self.assertEqual(evaluate("2 ** -1"), 0.5)
        self.assertEqual(evaluate("(-2) ** 2"), 4)

    def test_power_binds_tighter_than_multiplication(self):
        self.assertEqual(evaluate("2 * 3 ** 2"), 18)

    def test_division_by_zero(self):
        with self.assertRaises(ZeroDivisionError):
            evaluate("1 / 0")
        with self.assertRaises(ZeroDivisionError):
            evaluate("5 / (2 - 2)")

    def test_syntax_errors(self):
        for text in ("", "   ", "1 +", "* 2", "2 3", "(1 + 2", "1 + 2)", "()", "2 $ 3", "1 2 + 3", "abc"):
            with self.assertRaises(ValueError, msg=repr(text)):
                evaluate(text)
