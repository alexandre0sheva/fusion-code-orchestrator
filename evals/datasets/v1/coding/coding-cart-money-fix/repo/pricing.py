def line_total(price, qty):
    """The cost of ``qty`` units at ``price``."""
    return price * qty


def apply_discount(amount, percent):
    """``amount`` reduced by ``percent`` percent."""
    return amount - amount * percent / 100


def round_money(amount):
    """``amount`` rounded to cents."""
    return round(amount, 2)
