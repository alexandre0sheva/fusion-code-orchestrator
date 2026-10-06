from collections import OrderedDict


class LRUCache:
    """A cache of at most ``capacity`` entries that evicts the least recently used."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._items: OrderedDict = OrderedDict()

    def get(self, key, default=None):
        value = self._items.get(key)
        if not value:
            return default
        self._items.move_to_end(key)
        return value

    def put(self, key, value) -> None:
        if key in self._items:
            self._items.move_to_end(key)
        self._items[key] = value
        if len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def keys(self) -> list:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key) -> bool:
        if key in self._items:
            self._items.move_to_end(key)
            return True
        return False
