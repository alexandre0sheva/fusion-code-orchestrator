from decimal import Decimal, InvalidOperation


class InsufficientFunds(Exception):
    """The account cannot cover the withdrawal."""


def _money(value, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        raise TypeError("bad amount type")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("not a number") from exc
    if amount.as_tuple().exponent < -2:
        raise ValueError("at most two decimal places")
    if amount < 0 or (amount == 0 and not allow_zero):
        raise ValueError("amount must be greater than zero")
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

    def _apply(self, kind, signed, amount):
        self._balance += signed
        self.history.append((kind, amount, self._balance))
        return self._balance

    def deposit(self, amount):
        value = _money(amount)
        return self._apply("deposit", value, value)

    def withdraw(self, amount):
        value = _money(amount)
        if self._balance - value < -self.overdraft:
            raise InsufficientFunds("insufficient funds")
        return self._apply("withdraw", -value, value)


def transfer(src, dst, amount):
    value = _money(amount)
    if src is dst:
        raise ValueError("same account")
    dst._apply("transfer_in", value, value)
    if src._balance - value < -src.overdraft:
        raise InsufficientFunds("insufficient funds")
    src._apply("transfer_out", -value, value)
