import time


class TTLCache:
    def __init__(self, ttl, clock=time.monotonic):
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        self.ttl = ttl
        self._clock = clock
        self._data = {}

    def set(self, key, value):
        self._data[key] = (value, self._clock() + self.ttl)

    def get(self, key, default=None):
        entry = self._data.get(key)
        if entry is None:
            return default
        value, expires = entry
        if self._clock() >= expires:
            del self._data[key]
            return default
        return value

    def delete(self, key):
        return self._data.pop(key, None) is not None

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return key in self._data
