import heapq
import itertools


class Scheduler:
    def __init__(self):
        self._heap = []
        self._counter = itertools.count()

    def add(self, priority, task):
        heapq.heappush(self._heap, (priority, next(self._counter), task))

    def pop(self):
        if not self._heap:
            raise IndexError("pop from an empty scheduler")
        return heapq.heappop(self._heap)[2]

    def peek(self):
        if not self._heap:
            raise IndexError("peek at an empty scheduler")
        return self._heap[0][2]

    def remove(self, task):
        before = len(self._heap)
        self._heap = [entry for entry in self._heap if entry[2] != task]
        heapq.heapify(self._heap)
        return len(self._heap) != before

    def __len__(self):
        return len(self._heap)
