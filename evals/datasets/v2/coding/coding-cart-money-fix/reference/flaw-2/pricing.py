from decimal import Decimal


def line_total(price, qty):
    return Decimal(price) * qty


def apply_discount(amount, percent):
    return Decimal(amount) * (Decimal(100) - Decimal(percent)) / Decimal(100)


def round_money(amount):
    return Decimal(amount).quantize(Decimal("0.01"))
