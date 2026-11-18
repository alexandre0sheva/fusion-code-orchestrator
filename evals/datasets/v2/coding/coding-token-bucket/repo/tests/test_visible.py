import unittest

from bucket import TokenBucket


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class VisibleTests(unittest.TestCase):
    def test_starts_full_and_drains(self):
        bucket = TokenBucket(rate=1, capacity=2, clock=Clock())
        self.assertTrue(bucket.allow())
        self.assertTrue(bucket.allow())
        self.assertFalse(bucket.allow())

    def test_refills_with_time(self):
        clock = Clock()
        bucket = TokenBucket(rate=2, capacity=10, clock=clock)
        bucket.allow(10)
        clock.now += 1.5
        self.assertAlmostEqual(bucket.tokens(), 3.0)
        self.assertTrue(bucket.allow(3))
        self.assertFalse(bucket.allow())
