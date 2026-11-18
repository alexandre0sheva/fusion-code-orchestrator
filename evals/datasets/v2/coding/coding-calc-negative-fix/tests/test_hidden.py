import unittest

from calc.parser import evaluate


class EvaluateTests(unittest.TestCase):
    def test_spacing_never_matters(self):
        for text in ("5-3", "5 - 3", " 5 -3", "5- 3"):
            self.assertEqual(evaluate(text), 2, text)

    def test_subtraction_is_left_associative(self):
        self.assertEqual(evaluate("10 - 4 - 3"), 3)
        self.assertEqual(evaluate("10-4-3"), 3)

    def test_division_is_left_associative(self):
        self.assertEqual(evaluate("8 / 4 / 2"), 1)
        self.assertEqual(evaluate("100 / 10 * 5"), 50)

    def test_precedence_and_parentheses(self):
        self.assertEqual(evaluate("2 + 3 * 4"), 14)
        self.assertEqual(evaluate("(2 + 3) * 4"), 20)
        self.assertEqual(evaluate("2 * (3 + 4) - 1"), 13)

    def test_unary_minus(self):
        self.assertEqual(evaluate("-3"), -3)
        self.assertEqual(evaluate("--3"), 3)
        self.assertEqual(evaluate("-2 - 3"), -5)
        self.assertEqual(evaluate("2 * -3"), -6)
        self.assertEqual(evaluate("5 - -3"), 8)
        self.assertEqual(evaluate("5--3"), 8)
        self.assertEqual(evaluate("-(1 + 2)"), -3)
        self.assertEqual(evaluate("2 * -(1 + 2)"), -6)

    def test_unary_plus(self):
        self.assertEqual(evaluate("+4"), 4)
        self.assertEqual(evaluate("3 + +2"), 5)

    def test_unary_binds_tighter_than_multiplication(self):
        self.assertEqual(evaluate("-2 * 3"), -6)
        self.assertEqual(evaluate("6 / -2"), -3)

    def test_decimals(self):
        self.assertAlmostEqual(evaluate("1.5 + 2.25"), 3.75)
        self.assertAlmostEqual(evaluate("-1.5 * 2"), -3.0)

    def test_returns_a_float(self):
        self.assertIsInstance(evaluate("4 / 2"), float)

    def test_division_by_zero(self):
        for text in ("1 / 0", "5 / (3 - 3)", "2 / -0"):
            with self.assertRaises(ZeroDivisionError, msg=text):
                evaluate(text)

    def test_syntax_errors(self):
        for text in ("", "   ", "1 +", "* 2", "2 3", "(1 + 2", "1 + 2)", "()", "2 $ 3", "- ", "1 2 + 3"):
            with self.assertRaises(ValueError, msg=repr(text)):
                evaluate(text)
