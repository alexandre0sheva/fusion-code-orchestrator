import heapq


class Scheduler:
    def __init__(self):
        self._heap = []

    def add(self, priority, task):
        heapq.heappush(self._heap, (priority, str(task), task))

    def pop(self):
        if not self._heap:
            raise IndexError("pop from an empty scheduler")
        return heapq.heappop(self._heap)[2]

    def peek(self):
        if not self._heap:
            raise IndexError("peek at an empty scheduler")
        return self._heap[0][2]

    def remove(self, task):
        for index, entry in enumerate(self._heap):
            if entry[2] == task:
                del self._heap[index]
                return True
        return False

    def __len__(self):
        return len(self._heap)
