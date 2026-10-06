from decimal import Decimal

from pricing import apply_discount, line_total, round_money


class Cart:
    def __init__(self):
        self.lines = []
        self.discounts = []

    def add(self, sku, price, qty=1):
        if isinstance(price, bool) or not isinstance(price, (int, str, Decimal)):
            raise TypeError("price must be an int, str or Decimal")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise ValueError("qty must be a positive int")
        self.lines.append((sku, Decimal(price), qty))

    def add_discount(self, percent):
        if not 0 <= percent <= 100:
            raise ValueError("percent must be between 0 and 100")
        self.discounts.append(percent)

    def total(self):
        amount = sum((line_total(price, qty) for _, price, qty in self.lines), Decimal(0))
        for percent in self.discounts:
            amount = round_money(apply_discount(amount, percent))
        return round_money(amount)
