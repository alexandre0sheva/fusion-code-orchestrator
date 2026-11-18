import unittest

from ttlcache import TTLCache


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class TTLCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cache = TTLCache(10, self.clock)

    def test_ttl_must_be_positive(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                TTLCache(bad, self.clock)

    def test_live_until_the_deadline(self):
        self.cache.set("a", 1)
        self.clock.now = 109.999
        self.assertEqual(self.cache.get("a"), 1)

    def test_expired_at_exactly_the_deadline(self):
        self.cache.set("a", 1)
        self.clock.now = 110
        self.assertEqual(self.cache.get("a", "gone"), "gone")
        self.assertNotIn("a", self.cache)

    def test_setting_again_restarts_the_time(self):
        self.cache.set("a", 1)
        self.clock.now = 105
        self.cache.set("a", 2)
        self.clock.now = 112
        self.assertEqual(self.cache.get("a"), 2)
        self.clock.now = 115
        self.assertIsNone(self.cache.get("a"))

    def test_len_counts_only_live_entries(self):
        self.cache.set("a", 1)
        self.clock.now = 105
        self.cache.set("b", 2)
        self.assertEqual(len(self.cache), 2)
        self.clock.now = 110
        self.assertEqual(len(self.cache), 1)
        self.clock.now = 200
        self.assertEqual(len(self.cache), 0)

    def test_contains_checks_liveness(self):
        self.cache.set("a", 1)
        self.assertIn("a", self.cache)
        self.assertNotIn("b", self.cache)
        self.clock.now = 150
        self.assertNotIn("a", self.cache)

    def test_delete_only_reports_live_entries(self):
        self.cache.set("a", 1)
        self.assertTrue(self.cache.delete("a"))
        self.assertFalse(self.cache.delete("a"))
        self.cache.set("b", 2)
        self.clock.now = 120
        self.assertFalse(self.cache.delete("b"))

    def test_falsy_values_are_live_values(self):
        self.cache.set("zero", 0)
        self.assertEqual(self.cache.get("zero", "dflt"), 0)
        self.assertIn("zero", self.cache)
