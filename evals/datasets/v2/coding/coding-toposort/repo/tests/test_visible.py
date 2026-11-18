import unittest

from graph import CycleError, toposort


class VisibleTests(unittest.TestCase):
    def test_chain(self):
        self.assertEqual(toposort({"c": ["b"], "b": ["a"], "a": []}), ["a", "b", "c"])

    def test_cycle(self):
        with self.assertRaises(CycleError):
            toposort({"a": ["b"], "b": ["a"]})

    def test_ties_go_to_the_smallest_name(self):
        self.assertEqual(toposort({"c": ["a", "b"], "b": [], "a": []}), ["a", "b", "c"])
        self.assertEqual(toposort({"x": [], "m": [], "b": [], "a": ["x"]}), ["b", "m", "x", "a"])
