import unittest

from bfs import bfs_order


class VisibleTests(unittest.TestCase):
    def test_path(self):
        self.assertEqual(bfs_order({"a": ["b"], "b": ["c"]}, "a"), ["a", "b", "c"])

    def test_single_node(self):
        self.assertEqual(bfs_order({}, "a"), ["a"])
