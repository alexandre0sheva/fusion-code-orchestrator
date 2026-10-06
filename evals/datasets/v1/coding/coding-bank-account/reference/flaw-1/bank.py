from decimal import Decimal


class InsufficientFunds(Exception):
    """The account cannot cover the withdrawal."""


def _money(value, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        raise TypeError("bad amount type")
    amount = float(value)
    if amount < 0 or (amount == 0 and not allow_zero):
        raise ValueError("amount must be greater than zero")
    if round(amount, 2) != amount:
        raise ValueError("at most two decimal places")
    return amount


class Account:
    def __init__(self, owner, balance="0", overdraft="0"):
        self.owner = owner
        self._balance = _money(balance, allow_zero=True)
        self.overdraft = _money(overdraft, allow_zero=True)
        self.history = []

    @property
    def balance(self):
        return self._balance

    def _can_cover(self, amount):
        return self._balance - amount >= -self.overdraft

    def _apply(self, kind, signed, amount):
        self._balance += signed
        self.history.append((kind, amount, self._balance))
        return self._balance

    def deposit(self, amount):
        value = _money(amount)
        return self._apply("deposit", value, value)

    def withdraw(self, amount):
        value = _money(amount)
        if not self._can_cover(value):
            raise InsufficientFunds("insufficient funds")
        return self._apply("withdraw", -value, value)


def transfer(src, dst, amount):
    value = _money(amount)
    if src is dst:
        raise ValueError("same account")
    if not src._can_cover(value):
        raise InsufficientFunds("insufficient funds")
    src._apply("transfer_out", -value, value)
    dst._apply("transfer_in", value, value)
