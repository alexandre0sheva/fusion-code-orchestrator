import time


class TTLCache:
    def __init__(self, ttl, clock=time.monotonic):
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        self.ttl = ttl
        self._clock = clock
        self._data = {}

    def _purge(self):
        now = self._clock()
        for key in [k for k, (_, expires) in self._data.items() if now > expires]:
            del self._data[key]

    def set(self, key, value):
        self._data[key] = (value, self._clock() + self.ttl)

    def get(self, key, default=None):
        self._purge()
        entry = self._data.get(key)
        return default if entry is None else entry[0]

    def delete(self, key):
        self._purge()
        return self._data.pop(key, None) is not None

    def __len__(self):
        self._purge()
        return len(self._data)

    def __contains__(self, key):
        self._purge()
        return key in self._data
