import unittest

from scheduler import Scheduler


class SchedulerTests(unittest.TestCase):
    def test_priority_order(self):
        s = Scheduler()
        for priority, name in ((3, "c"), (1, "a"), (2, "b")):
            s.add(priority, name)
        self.assertEqual([s.pop(), s.pop(), s.pop()], ["a", "b", "c"])

    def test_equal_priorities_are_fifo(self):
        s = Scheduler()
        for name in ("z", "m", "a", "q"):
            s.add(1, name)
        self.assertEqual([s.pop() for _ in range(4)], ["z", "m", "a", "q"])

    def test_tasks_are_never_compared(self):
        s = Scheduler()
        first, second = {"id": 1}, {"id": 2}
        s.add(1, first)
        s.add(1, second)
        self.assertIs(s.pop(), first)
        self.assertIs(s.pop(), second)

    def test_empty_scheduler(self):
        s = Scheduler()
        with self.assertRaises(IndexError):
            s.pop()
        with self.assertRaises(IndexError):
            s.peek()

    def test_peek_does_not_remove(self):
        s = Scheduler()
        s.add(2, "b")
        s.add(1, "a")
        self.assertEqual(s.peek(), "a")
        self.assertEqual(len(s), 2)
        self.assertEqual(s.pop(), "a")

    def test_remove_cancels_only_the_earliest_matching_task(self):
        s = Scheduler()
        s.add(1, "x")
        s.add(2, "y")
        s.add(3, "x")
        self.assertTrue(s.remove("x"))
        self.assertEqual(len(s), 2)
        self.assertEqual([s.pop(), s.pop()], ["y", "x"])

    def test_remove_missing(self):
        s = Scheduler()
        s.add(1, "x")
        self.assertFalse(s.remove("nope"))
        self.assertEqual(len(s), 1)

    def test_remove_keeps_the_order_of_the_rest(self):
        s = Scheduler()
        for priority in (5, 3, 8, 1, 9, 2, 7):
            s.add(priority, f"t{priority}")
        s.remove("t3")
        s.remove("t9")
        self.assertEqual([s.pop() for _ in range(5)], ["t1", "t2", "t5", "t7", "t8"])

    def test_removed_task_can_be_added_again(self):
        s = Scheduler()
        s.add(1, "x")
        s.remove("x")
        s.add(2, "x")
        self.assertEqual(len(s), 1)
        self.assertEqual(s.pop(), "x")
        self.assertEqual(len(s), 0)

    def test_peek_skips_cancelled_tasks(self):
        s = Scheduler()
        s.add(1, "a")
        s.add(2, "b")
        s.remove("a")
        self.assertEqual(s.peek(), "b")
