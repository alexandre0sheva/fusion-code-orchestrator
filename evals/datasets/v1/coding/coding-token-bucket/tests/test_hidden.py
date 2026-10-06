import unittest

from bucket import TokenBucket


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class TokenBucketTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()

    def test_validation(self):
        for rate, capacity in ((0, 1), (-1, 1), (1, 0)):
            with self.assertRaises(ValueError):
                TokenBucket(rate, capacity, self.clock)

    def test_starts_full(self):
        bucket = TokenBucket(2, 5, self.clock)
        self.assertEqual(bucket.tokens(), 5)

    def test_drains_and_refuses(self):
        bucket = TokenBucket(1, 3, self.clock)
        self.assertTrue(bucket.allow(3))
        self.assertFalse(bucket.allow())
        self.assertEqual(bucket.tokens(), 0)

    def test_refills_with_time(self):
        bucket = TokenBucket(2, 10, self.clock)
        bucket.allow(10)
        self.clock.now += 1.5
        self.assertAlmostEqual(bucket.tokens(), 3.0)
        self.assertTrue(bucket.allow(3))
        self.assertFalse(bucket.allow())

    def test_refill_is_capped_at_capacity(self):
        bucket = TokenBucket(5, 4, self.clock)
        bucket.allow(4)
        self.clock.now += 1000
        self.assertEqual(bucket.tokens(), 4)

    def test_repeated_reads_do_not_add_tokens(self):
        bucket = TokenBucket(1, 10, self.clock)
        bucket.allow(10)
        self.clock.now += 2
        self.assertAlmostEqual(bucket.tokens(), 2.0)
        self.assertAlmostEqual(bucket.tokens(), 2.0)
        self.clock.now += 1
        self.assertAlmostEqual(bucket.tokens(), 3.0)

    def test_refused_request_takes_nothing(self):
        bucket = TokenBucket(1, 5, self.clock)
        bucket.allow(4)
        self.assertFalse(bucket.allow(2))
        self.assertAlmostEqual(bucket.tokens(), 1.0)

    def test_wait_time(self):
        bucket = TokenBucket(2, 10, self.clock)
        bucket.allow(10)
        self.assertAlmostEqual(bucket.wait_time(4), 2.0)
        self.clock.now += 1
        self.assertAlmostEqual(bucket.wait_time(4), 1.0)
        self.clock.now += 5
        self.assertEqual(bucket.wait_time(4), 0.0)
        self.assertAlmostEqual(bucket.tokens(), 10.0)

    def test_bad_cost(self):
        bucket = TokenBucket(1, 3, self.clock)
        for cost in (0, -1, 4):
            with self.assertRaises(ValueError):
                bucket.allow(cost)
