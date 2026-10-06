from pricing import apply_discount, line_total, round_money


class Cart:
    def __init__(self):
        self.lines = []
        self.discounts = []

    def add(self, sku, price, qty=1):
        self.lines.append((sku, price, qty))

    def add_discount(self, percent):
        self.discounts.append(percent)

    def total(self):
        amount = sum(line_total(price, qty) for _, price, qty in self.lines)
        for percent in self.discounts:
            amount = round_money(apply_discount(amount, percent))
        return round_money(amount)
