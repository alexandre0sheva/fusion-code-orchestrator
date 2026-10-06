import unittest

from ringbuffer import RingBuffer
from collections import deque


class VisibleTests(unittest.TestCase):
    def test_append_and_to_list(self):
        buffer = RingBuffer(3)
        buffer.append(1)
        buffer.append(2)
        self.assertEqual(buffer.to_list(), [1, 2])

    def test_peek(self):
        buffer = RingBuffer(2)
        buffer.append("x")
        self.assertEqual(buffer.peek(), "x")

    def test_overwrites_the_oldest_when_full(self):
        buffer = RingBuffer(3)
        for n in range(1, 6):
            buffer.append(n)
        self.assertEqual(buffer.to_list(), [3, 4, 5])
        self.assertEqual(len(buffer), 3)
        self.assertTrue(buffer.is_full())
