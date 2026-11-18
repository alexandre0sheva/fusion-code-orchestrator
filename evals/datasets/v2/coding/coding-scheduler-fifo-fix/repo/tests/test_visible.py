import unittest

from scheduler import Scheduler


class VisibleTests(unittest.TestCase):
    def test_lowest_priority_first(self):
        s = Scheduler()
        s.add(5, "low")
        s.add(1, "high")
        self.assertEqual(s.pop(), "high")

    def test_empty_pop(self):
        with self.assertRaises(IndexError):
            Scheduler().pop()

    def test_remove_cancels_only_the_earliest_matching_task(self):
        s = Scheduler()
        s.add(1, "x")
        s.add(2, "y")
        s.add(3, "x")
        self.assertTrue(s.remove("x"))
        self.assertEqual(len(s), 2)
        self.assertEqual([s.pop(), s.pop()], ["y", "x"])
