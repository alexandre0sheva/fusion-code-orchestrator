import unittest

from ttlcache import TTLCache


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class VisibleTests(unittest.TestCase):
    def test_get_before_expiry(self):
        clock = Clock()
        cache = TTLCache(10, clock)
        cache.set("a", 1)
        clock.now = 5
        self.assertEqual(cache.get("a"), 1)

    def test_missing(self):
        self.assertEqual(TTLCache(10, Clock()).get("zzz", "none"), "none")

    def test_len_ignores_expired_entries(self):
        clock = Clock()
        cache = TTLCache(10, clock)
        cache.set("a", 1)
        clock.now = 10
        self.assertEqual(len(cache), 0)
