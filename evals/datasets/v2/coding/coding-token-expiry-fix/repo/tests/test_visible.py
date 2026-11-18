import unittest
from datetime import datetime, timedelta, timezone

from tokens import expires_at, is_expired


class VisibleTests(unittest.TestCase):
    def test_expires_at(self):
        issued = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(expires_at(issued, 60), issued + timedelta(seconds=60))

    def test_not_expired_yet(self):
        issued = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        now = datetime(2024, 1, 1, 12, 0, 30, tzinfo=timezone.utc)
        self.assertFalse(is_expired(issued, 60, now=now))

    def test_expired_at_exactly_the_deadline(self):
        issued = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(is_expired(issued, 60, now=issued + timedelta(seconds=60)))
