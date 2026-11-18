import unittest
from decimal import Decimal

from bank import Account, InsufficientFunds, transfer


class AccountTests(unittest.TestCase):
    def test_money_is_exact(self):
        account = Account("ann")
        account.deposit("0.10")
        account.deposit("0.20")
        self.assertEqual(account.balance, Decimal("0.30"))

    def test_amount_types(self):
        account = Account("ann", 5)
        self.assertEqual(account.deposit(2), Decimal("7"))
        self.assertEqual(account.deposit(Decimal("0.5")), Decimal("7.5"))
        for bad in (1.5, True):
            with self.assertRaises(TypeError):
                account.deposit(bad)

    def test_amount_values(self):
        account = Account("ann", 5)
        for bad in (0, -1, "0", "-3.00", "1.234", Decimal("0.001")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                account.deposit(bad)
        self.assertEqual(account.balance, Decimal("5"))

    def test_opening_values(self):
        self.assertEqual(Account("a", 0).balance, Decimal("0"))
        with self.assertRaises(ValueError):
            Account("a", "-1")
        with self.assertRaises(ValueError):
            Account("a", 0, "-5")
        with self.assertRaises(TypeError):
            Account("a", 1.5)

    def test_withdraw_and_overdraft(self):
        account = Account("ann", "10", overdraft="5")
        self.assertEqual(account.withdraw("12.50"), Decimal("-2.50"))
        with self.assertRaises(InsufficientFunds):
            account.withdraw("2.51")
        self.assertEqual(account.balance, Decimal("-2.50"))
        self.assertEqual(account.withdraw("2.50"), Decimal("-5.00"))

    def test_withdraw_without_overdraft(self):
        account = Account("ann", "10")
        with self.assertRaises(InsufficientFunds):
            account.withdraw("10.01")
        self.assertEqual(account.history, [])

    def test_history(self):
        account = Account("ann", "10")
        account.deposit("5")
        account.withdraw("3")
        self.assertEqual(
            account.history,
            [("deposit", Decimal("5"), Decimal("15")), ("withdraw", Decimal("3"), Decimal("12"))],
        )

    def test_transfer(self):
        a, b = Account("a", "100"), Account("b", "5")
        transfer(a, b, "30.25")
        self.assertEqual(a.balance, Decimal("69.75"))
        self.assertEqual(b.balance, Decimal("35.25"))
        self.assertEqual(a.history[-1], ("transfer_out", Decimal("30.25"), Decimal("69.75")))
        self.assertEqual(b.history[-1], ("transfer_in", Decimal("30.25"), Decimal("35.25")))

    def test_failed_transfer_changes_nothing(self):
        a, b = Account("a", "10"), Account("b", "5")
        with self.assertRaises(InsufficientFunds):
            transfer(a, b, "10.01")
        self.assertEqual(a.balance, Decimal("10"))
        self.assertEqual(b.balance, Decimal("5"))
        self.assertEqual(a.history, [])
        self.assertEqual(b.history, [])

    def test_transfer_to_self(self):
        a = Account("a", "10")
        with self.assertRaises(ValueError):
            transfer(a, a, "1")

    def test_transfer_validates_the_amount_first(self):
        a, b = Account("a", "10"), Account("b", "5")
        with self.assertRaises(ValueError):
            transfer(a, b, "-1")
        with self.assertRaises(TypeError):
            transfer(a, b, 2.5)
        self.assertEqual((a.balance, b.balance), (Decimal("10"), Decimal("5")))
