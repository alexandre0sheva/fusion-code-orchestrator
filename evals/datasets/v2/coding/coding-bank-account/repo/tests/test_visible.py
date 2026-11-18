import unittest
from decimal import Decimal

from bank import Account
from bank import Account, InsufficientFunds, transfer


class VisibleTests(unittest.TestCase):
    def test_deposit(self):
        account = Account("ann", "10.00")
        self.assertEqual(account.deposit("5.50"), Decimal("15.50"))

    def test_history(self):
        account = Account("ann")
        account.deposit(3)
        self.assertEqual(account.history, [("deposit", Decimal("3"), Decimal("3"))])

    def test_money_is_exact(self):
        account = Account("ann")
        account.deposit("0.10")
        account.deposit("0.20")
        self.assertEqual(account.balance, Decimal("0.30"))
