from decimal import Decimal


class InsufficientFunds(Exception):
    """The account cannot cover the withdrawal."""


class Account:
    def __init__(self, owner: str, balance="0", overdraft="0") -> None:
        raise NotImplementedError

    def deposit(self, amount) -> Decimal:
        raise NotImplementedError

    def withdraw(self, amount) -> Decimal:
        raise NotImplementedError


def transfer(src: Account, dst: Account, amount) -> None:
    raise NotImplementedError
