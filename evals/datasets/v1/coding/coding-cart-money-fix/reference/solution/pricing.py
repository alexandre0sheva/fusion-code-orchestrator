from decimal import ROUND_HALF_UP, Decimal


def line_total(price, qty):
    """The cost of ``qty`` units at ``price``."""
    return Decimal(price) * qty


def apply_discount(amount, percent):
    """``amount`` reduced by ``percent`` percent."""
    return Decimal(amount) * (Decimal(100) - Decimal(percent)) / Decimal(100)


def round_money(amount):
    """``amount`` rounded to cents."""
    return Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
