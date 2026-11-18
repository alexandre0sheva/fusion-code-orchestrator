from decimal import Decimal, InvalidOperation


class InsufficientFunds(Exception):
    """The account cannot cover the withdrawal."""


def _money(value, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        raise TypeError(f"amount must be an int, str or Decimal, not {type(value).__name__}")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc
    if not amount.is_finite() or amount.as_tuple().exponent < -2:
        raise ValueError("at most two decimal places are allowed")
    if amount < 0 or (amount == 0 and not allow_zero):
        raise ValueError("amount must be greater than zero")
    return amount


class Account:
    def __init__(self, owner: str, balance="0", overdraft="0") -> None:
        self.owner = owner
        self._balance = _money(balance, allow_zero=True)
        self.overdraft = _money(overdraft, allow_zero=True)
        self.history: list[tuple[str, Decimal, Decimal]] = []

    @property
    def balance(self) -> Decimal:
        return self._balance

    def _can_cover(self, amount: Decimal) -> bool:
        return self._balance - amount >= -self.overdraft

    def _apply(self, kind: str, signed: Decimal, amount: Decimal) -> Decimal:
        self._balance += signed
        self.history.append((kind, amount, self._balance))
        return self._balance

    def deposit(self, amount) -> Decimal:
        value = _money(amount)
        return self._apply("deposit", value, value)

    def withdraw(self, amount) -> Decimal:
        value = _money(amount)
        if not self._can_cover(value):
            raise InsufficientFunds(f"{self.owner} cannot withdraw {value}")
        return self._apply("withdraw", -value, value)


def transfer(src: Account, dst: Account, amount) -> None:
    value = _money(amount)
    if src is dst:
        raise ValueError("cannot transfer to the same account")
    if not src._can_cover(value):
        raise InsufficientFunds(f"{src.owner} cannot transfer {value}")
    src._apply("transfer_out", -value, value)
    dst._apply("transfer_in", value, value)
