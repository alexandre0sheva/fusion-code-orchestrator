class RingBuffer:
    """A fixed-capacity queue that overwrites its oldest item when full."""

    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._buf = [None] * capacity
        self._start = 0
        self._size = 0

    def append(self, item):
        if self._size == self.capacity:
            raise OverflowError("buffer is full")
        self._buf[(self._start + self._size) % self.capacity] = item
        self._size += 1

    def pop(self):
        if self._size == 0:
            raise IndexError("pop from an empty buffer")
        item = self._buf[self._start]
        self._start += 1
        return item

    def peek(self):
        if self._size == 0:
            raise IndexError("peek at an empty buffer")
        return self._buf[self._start]

    def to_list(self):
        return [self._buf[(self._start + i) % self.capacity] for i in range(self._size)]

    def is_full(self):
        return self._size == self.capacity

    def clear(self):
        self._start = 0
        self._size = 0

    def __len__(self):
        return self._size
