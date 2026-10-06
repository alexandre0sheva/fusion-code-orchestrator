import random
import unittest

from bst import BST


def build(*keys):
    tree = BST()
    for key in keys:
        tree.insert(key)
    return tree


class BSTTests(unittest.TestCase):
    def test_insert_reports_new_keys_and_len_counts_them(self):
        tree = BST()
        self.assertTrue(tree.insert(5))
        self.assertFalse(tree.insert(5))
        self.assertTrue(tree.insert(3))
        self.assertEqual(len(tree), 2)
        self.assertEqual(tree.inorder(), [3, 5])

    def test_delete_missing(self):
        tree = build(5, 3)
        self.assertFalse(tree.delete(99))
        self.assertFalse(BST().delete(1))
        self.assertEqual(len(tree), 2)

    def test_delete_leaf_and_one_child(self):
        tree = build(5, 3, 8, 2, 9)
        self.assertTrue(tree.delete(2))
        self.assertTrue(tree.delete(8))  # one child (9)
        self.assertEqual(tree.inorder(), [3, 5, 9])
        self.assertEqual(len(tree), 3)

    def test_delete_a_node_with_two_children(self):
        tree = build(10, 5, 15, 3, 7, 12, 20)
        self.assertTrue(tree.delete(5))
        self.assertEqual(tree.inorder(), [3, 7, 10, 12, 15, 20])
        self.assertFalse(tree.contains(5))
        self.assertEqual(len(tree), 6)

    def test_delete_the_root_with_two_children(self):
        tree = build(10, 5, 15, 3, 7, 12, 20)
        self.assertTrue(tree.delete(10))
        self.assertEqual(tree.inorder(), [3, 5, 7, 12, 15, 20])
        self.assertFalse(tree.contains(10))
        for key in (3, 5, 7, 12, 15, 20):
            self.assertTrue(tree.contains(key))

    def test_delete_until_empty(self):
        keys = [8, 4, 12, 2, 6, 10, 14]
        tree = build(*keys)
        for key in keys:
            self.assertTrue(tree.delete(key))
            self.assertFalse(tree.contains(key))
        self.assertEqual((tree.inorder(), len(tree)), ([], 0))

    def test_agrees_with_a_set_under_random_operations(self):
        rng = random.Random(1234)
        tree, model = BST(), set()
        for _ in range(600):
            key = rng.randrange(60)
            if rng.random() < 0.55:
                self.assertEqual(tree.insert(key), key not in model)
                model.add(key)
            else:
                self.assertEqual(tree.delete(key), key in model)
                model.discard(key)
            self.assertEqual(tree.inorder(), sorted(model))
            self.assertEqual(len(tree), len(model))
        for key in range(60):
            self.assertEqual(tree.contains(key), key in model)
