import unittest
from collections import deque

from ringbuffer import RingBuffer


class RingBufferTests(unittest.TestCase):
    def test_capacity_must_be_positive(self):
        with self.assertRaises(ValueError):
            RingBuffer(0)

    def test_overwrites_the_oldest_when_full(self):
        buffer = RingBuffer(3)
        for n in range(1, 6):
            buffer.append(n)
        self.assertEqual(buffer.to_list(), [3, 4, 5])
        self.assertEqual(len(buffer), 3)
        self.assertTrue(buffer.is_full())

    def test_pop_returns_oldest_first_and_shrinks(self):
        buffer = RingBuffer(3)
        for n in (1, 2, 3):
            buffer.append(n)
        self.assertEqual(buffer.pop(), 1)
        self.assertEqual(len(buffer), 2)
        self.assertFalse(buffer.is_full())
        self.assertEqual(buffer.to_list(), [2, 3])

    def test_wraps_around_after_pops(self):
        buffer = RingBuffer(3)
        for n in (1, 2, 3):
            buffer.append(n)
        buffer.pop()
        buffer.pop()
        buffer.append(4)
        buffer.append(5)
        self.assertEqual(buffer.to_list(), [3, 4, 5])
        self.assertEqual([buffer.pop() for _ in range(3)], [3, 4, 5])
        self.assertEqual(len(buffer), 0)

    def test_empty_buffer(self):
        buffer = RingBuffer(2)
        with self.assertRaises(IndexError):
            buffer.pop()
        with self.assertRaises(IndexError):
            buffer.peek()
        self.assertEqual(buffer.to_list(), [])

    def test_peek_does_not_remove(self):
        buffer = RingBuffer(2)
        buffer.append("a")
        buffer.append("b")
        buffer.append("c")  # overwrites a
        self.assertEqual(buffer.peek(), "b")
        self.assertEqual(len(buffer), 2)

    def test_clear(self):
        buffer = RingBuffer(2)
        buffer.append(1)
        buffer.append(2)
        buffer.clear()
        self.assertEqual((len(buffer), buffer.to_list()), (0, []))
        buffer.append(9)
        self.assertEqual(buffer.to_list(), [9])

    def test_capacity_one(self):
        buffer = RingBuffer(1)
        buffer.append("a")
        buffer.append("b")
        self.assertEqual(buffer.to_list(), ["b"])
        self.assertEqual(buffer.pop(), "b")
        with self.assertRaises(IndexError):
            buffer.pop()

    def test_matches_a_deque_under_mixed_operations(self):
        import random

        rng = random.Random(7)
        buffer, model = RingBuffer(4), deque(maxlen=4)
        for step in range(500):
            if rng.random() < 0.6 or not model:
                buffer.append(step)
                model.append(step)
            else:
                self.assertEqual(buffer.pop(), model.popleft())
            self.assertEqual(buffer.to_list(), list(model))
            self.assertEqual(len(buffer), len(model))
