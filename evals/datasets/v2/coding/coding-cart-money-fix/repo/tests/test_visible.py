import unittest
from decimal import Decimal

from cart import Cart
import pricing


class VisibleTests(unittest.TestCase):
    def test_simple_total(self):
        cart = Cart()
        cart.add("pen", "1.50", 2)
        cart.add("pad", "3.00")
        self.assertEqual(cart.total(), Decimal("6.00"))

    def test_empty_cart(self):
        self.assertEqual(Cart().total(), Decimal("0.00"))

    def test_discounts_compound_without_intermediate_rounding(self):
        cart = Cart()
        cart.add("a", "10.05")
        cart.add_discount(10)
        cart.add_discount(10)
        self.assertEqual(cart.total(), Decimal("8.14"))  # 10.05 * 0.9 * 0.9 = 8.1405
