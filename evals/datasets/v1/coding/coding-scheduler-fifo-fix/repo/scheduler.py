import heapq


class Scheduler:
    """A priority queue of tasks."""

    def __init__(self):
        self._heap = []

    def add(self, priority, task):
        heapq.heappush(self._heap, (priority, task))

    def pop(self):
        if not self._heap:
            raise IndexError("pop from an empty scheduler")
        return heapq.heappop(self._heap)[1]

    def peek(self):
        if not self._heap:
            raise IndexError("peek at an empty scheduler")
        return self._heap[0][1]

    def remove(self, task):
        before = len(self._heap)
        self._heap = [entry for entry in self._heap if entry[1] != task]
        return len(self._heap) != before

    def __len__(self):
        return len(self._heap)
