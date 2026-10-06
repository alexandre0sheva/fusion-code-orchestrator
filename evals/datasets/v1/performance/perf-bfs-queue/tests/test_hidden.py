import unittest

from bfs import bfs_order


class HiddenTests(unittest.TestCase):
    def test_levels_before_depth(self):
        graph = {1: [2, 3], 2: [4], 3: [5], 4: [], 5: []}
        self.assertEqual(bfs_order(graph, 1), [1, 2, 3, 4, 5])

    def test_neighbours_in_listed_order(self):
        self.assertEqual(bfs_order({"r": ["z", "a", "m"]}, "r"), ["r", "z", "a", "m"])

    def test_cycles_do_not_repeat_nodes(self):
        graph = {1: [2], 2: [3], 3: [1, 2]}
        self.assertEqual(bfs_order(graph, 1), [1, 2, 3])

    def test_unreachable_nodes_are_left_out(self):
        self.assertEqual(bfs_order({1: [2], 3: [4]}, 1), [1, 2])

    def test_node_missing_from_the_graph_has_no_neighbours(self):
        self.assertEqual(bfs_order({1: [9]}, 1), [1, 9])

    def test_diamond(self):
        graph = {"a": ["b", "c"], "b": ["d"], "c": ["d"]}
        self.assertEqual(bfs_order(graph, "a"), ["a", "b", "c", "d"])
