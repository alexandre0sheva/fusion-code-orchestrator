import unittest
from decimal import Decimal

import pricing
from cart import Cart


class CartTests(unittest.TestCase):
    def test_total_is_a_two_place_decimal(self):
        cart = Cart()
        cart.add("a", "19.99", 3)
        total = cart.total()
        self.assertIsInstance(total, Decimal)
        self.assertEqual(total, Decimal("59.97"))
        self.assertEqual(str(total), "59.97")

    def test_empty_cart(self):
        self.assertEqual(str(Cart().total()), "0.00")

    def test_rounding_is_half_up(self):
        cart = Cart()
        cart.add("x", "2.675")
        self.assertEqual(cart.total(), Decimal("2.68"))
        other = Cart()
        other.add("y", "2.665")
        self.assertEqual(other.total(), Decimal("2.67"))

    def test_one_discount(self):
        cart = Cart()
        cart.add("a", "19.99", 3)
        cart.add_discount(10)
        self.assertEqual(cart.total(), Decimal("53.97"))

    def test_discounts_compound_without_intermediate_rounding(self):
        cart = Cart()
        cart.add("a", "10.05")
        cart.add_discount(10)
        cart.add_discount(10)
        self.assertEqual(cart.total(), Decimal("8.14"))  # 10.05 * 0.9 * 0.9 = 8.1405

    def test_discount_edges(self):
        cart = Cart()
        cart.add("a", "9.99")
        cart.add_discount(0)
        self.assertEqual(cart.total(), Decimal("9.99"))
        cart.add_discount(100)
        self.assertEqual(cart.total(), Decimal("0.00"))
        for bad in (-1, 101):
            with self.assertRaises(ValueError):
                cart.add_discount(bad)

    def test_price_types(self):
        cart = Cart()
        cart.add("a", 2)
        cart.add("b", Decimal("0.10"), 3)
        self.assertEqual(cart.total(), Decimal("2.30"))
        with self.assertRaises(TypeError):
            cart.add("c", 1.5)

    def test_quantity_validation(self):
        cart = Cart()
        for bad in (0, -1, 1.5, "2"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                cart.add("a", "1.00", bad)

    def test_pricing_functions_keep_full_precision(self):
        self.assertEqual(pricing.line_total(Decimal("0.10"), 3), Decimal("0.30"))
        self.assertEqual(pricing.apply_discount(Decimal("59.97"), 10), Decimal("53.973"))
        self.assertEqual(pricing.round_money(Decimal("53.973")), Decimal("53.97"))
        self.assertEqual(pricing.round_money(Decimal("2.675")), Decimal("2.68"))
