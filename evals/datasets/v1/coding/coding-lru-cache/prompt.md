Implement the class `LRUCache` in `lru.py`: a fixed-size cache that drops the least recently used entry.

- `LRUCache(capacity)`: `capacity` must be at least 1, else `ValueError`.
- `get(key, default=None)` returns the value, or `default` if the key is absent. A hit makes the key the most recently used.
- `put(key, value)` stores the value and makes the key the most recently used. Updating an existing key never evicts anything. Adding a new key to a full cache first evicts the least recently used key.
- `key in cache` and `len(cache)` work. Checking `in` does **not** change the order.
- `keys()` returns a list of the keys from least to most recently used.
