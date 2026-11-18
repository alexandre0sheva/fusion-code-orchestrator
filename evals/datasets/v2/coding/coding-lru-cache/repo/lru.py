class LRUCache:
    """A cache of at most ``capacity`` entries that evicts the least recently used."""

    def __init__(self, capacity: int) -> None:
        raise NotImplementedError

    def get(self, key, default=None):
        raise NotImplementedError

    def put(self, key, value) -> None:
        raise NotImplementedError

    def keys(self) -> list:
        raise NotImplementedError

    def __len__(self) -> int:
        raise NotImplementedError

    def __contains__(self, key) -> bool:
        raise NotImplementedError
