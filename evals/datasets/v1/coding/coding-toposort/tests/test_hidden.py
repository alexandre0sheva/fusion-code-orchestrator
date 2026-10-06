import unittest

from graph import CycleError, toposort


class ToposortTests(unittest.TestCase):
    def test_chain(self):
        self.assertEqual(toposort({"c": ["b"], "b": ["a"], "a": []}), ["a", "b", "c"])

    def test_diamond(self):
        graph = {"app": ["lib", "util"], "lib": ["core"], "util": ["core"]}
        self.assertEqual(toposort(graph), ["core", "lib", "util", "app"])

    def test_nodes_that_only_appear_as_dependencies_are_included(self):
        self.assertEqual(toposort({"a": ["z"]}), ["z", "a"])

    def test_ties_go_to_the_smallest_name(self):
        self.assertEqual(toposort({"c": ["a", "b"], "b": [], "a": []}), ["a", "b", "c"])
        self.assertEqual(toposort({"x": [], "m": [], "b": [], "a": ["x"]}), ["b", "m", "x", "a"])

    def test_empty_graph(self):
        self.assertEqual(toposort({}), [])

    def test_cycle(self):
        with self.assertRaises(CycleError):
            toposort({"a": ["b"], "b": ["c"], "c": ["a"]})

    def test_self_dependency_is_a_cycle(self):
        with self.assertRaises(CycleError):
            toposort({"a": ["a"]})

    def test_cycle_beside_valid_nodes(self):
        with self.assertRaises(CycleError):
            toposort({"ok": [], "a": ["b"], "b": ["a"]})

    def test_duplicate_dependencies_and_no_mutation(self):
        graph = {"b": ["a", "a"], "a": []}
        self.assertEqual(toposort(graph), ["a", "b"])
        self.assertEqual(graph, {"b": ["a", "a"], "a": []})
