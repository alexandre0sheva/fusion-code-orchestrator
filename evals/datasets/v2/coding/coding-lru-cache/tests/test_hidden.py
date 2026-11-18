import unittest

from lru import LRUCache


class LRUCacheTests(unittest.TestCase):
    def test_capacity_must_be_positive(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                LRUCache(bad)

    def test_get_default(self):
        cache = LRUCache(2)
        self.assertEqual(cache.get("x", "dflt"), "dflt")

    def test_eviction_order(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        self.assertEqual(cache.keys(), ["b", "c"])
        self.assertEqual(len(cache), 2)

    def test_get_refreshes_recency(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        self.assertEqual(cache.get("a"), 1)
        cache.put("c", 3)
        self.assertIn("a", cache)
        self.assertNotIn("b", cache)

    def test_update_existing_does_not_evict_and_refreshes(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("a", 10)
        self.assertEqual(len(cache), 2)
        self.assertEqual(cache.keys(), ["b", "a"])
        self.assertEqual(cache.get("a"), 10)

    def test_contains_does_not_refresh(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        self.assertIn("a", cache)
        cache.put("c", 3)
        self.assertNotIn("a", cache)

    def test_capacity_one(self):
        cache = LRUCache(1)
        cache.put("a", 1)
        cache.put("b", 2)
        self.assertEqual(cache.keys(), ["b"])

    def test_falsy_values_are_stored(self):
        cache = LRUCache(2)
        cache.put("zero", 0)
        self.assertEqual(cache.get("zero", "dflt"), 0)
