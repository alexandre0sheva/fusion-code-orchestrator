import heapq
import itertools


class Scheduler:
    """A priority queue of tasks."""

    def __init__(self):
        self._heap = []
        self._counter = itertools.count()
        self._cancelled = set()
        self._pending = 0

    def add(self, priority, task):
        heapq.heappush(self._heap, (priority, next(self._counter), task))
        self._pending += 1

    def _drop_cancelled(self):
        while self._heap and self._heap[0][1] in self._cancelled:
            self._cancelled.discard(self._heap[0][1])
            heapq.heappop(self._heap)

    def pop(self):
        self._drop_cancelled()
        if not self._heap:
            raise IndexError("pop from an empty scheduler")
        self._pending -= 1
        return heapq.heappop(self._heap)[2]

    def peek(self):
        self._drop_cancelled()
        if not self._heap:
            raise IndexError("peek at an empty scheduler")
        return self._heap[0][2]

    def remove(self, task):
        candidates = [e for e in self._heap if e[1] not in self._cancelled and e[2] == task]
        if not candidates:
            return False
        self._cancelled.add(min(candidates)[1])
        self._pending -= 1
        return True

    def __len__(self):
        return self._pending
