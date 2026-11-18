import unittest
from datetime import datetime, timedelta, timezone

from tokens import expires_at, is_expired

UTC = timezone.utc
ISSUED = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


class ExpiryTests(unittest.TestCase):
    def test_expires_at_is_utc_aware(self):
        result = expires_at(ISSUED, 3600)
        self.assertEqual(result, datetime(2024, 1, 1, 13, 0, tzinfo=UTC))
        self.assertEqual(result.utcoffset(), timedelta(0))

    def test_naive_issue_time_is_taken_as_utc(self):
        result = expires_at(datetime(2024, 1, 1, 12, 0), 60)
        self.assertEqual(result, datetime(2024, 1, 1, 12, 1, tzinfo=UTC))
        self.assertIsNotNone(result.tzinfo)

    def test_other_timezones_are_converted(self):
        plus_two = timezone(timedelta(hours=2))
        issued = datetime(2024, 1, 1, 12, 0, tzinfo=plus_two)  # 10:00 UTC
        result = expires_at(issued, 3600)
        self.assertEqual(result, datetime(2024, 1, 1, 11, 0, tzinfo=UTC))
        self.assertEqual(result.utcoffset(), timedelta(0))

    def test_negative_ttl(self):
        with self.assertRaises(ValueError):
            expires_at(ISSUED, -1)
        with self.assertRaises(ValueError):
            is_expired(ISSUED, -5, now=ISSUED)

    def test_before_at_and_after_the_deadline(self):
        self.assertFalse(is_expired(ISSUED, 60, now=ISSUED + timedelta(seconds=59)))
        self.assertTrue(is_expired(ISSUED, 60, now=ISSUED + timedelta(seconds=60)))
        self.assertTrue(is_expired(ISSUED, 60, now=ISSUED + timedelta(seconds=61)))

    def test_zero_ttl_expires_immediately(self):
        self.assertTrue(is_expired(ISSUED, 0, now=ISSUED))

    def test_naive_now_is_taken_as_utc(self):
        self.assertTrue(is_expired(ISSUED, 60, now=datetime(2024, 1, 1, 12, 5)))
        self.assertFalse(is_expired(ISSUED, 600, now=datetime(2024, 1, 1, 12, 5)))

    def test_now_in_another_timezone(self):
        minus_five = timezone(timedelta(hours=-5))
        now = datetime(2024, 1, 1, 7, 30, tzinfo=minus_five)  # 12:30 UTC
        self.assertTrue(is_expired(ISSUED, 1800, now=now))
        self.assertFalse(is_expired(ISSUED, 1801, now=now))

    def test_default_now_is_the_current_utc_time(self):
        long_ago = datetime(2000, 1, 1, tzinfo=UTC)
        self.assertTrue(is_expired(long_ago, 60))
        fresh = datetime.now(UTC)
        self.assertFalse(is_expired(fresh, 3600))
        self.assertFalse(is_expired(datetime.now(UTC).replace(tzinfo=None), 3600))
