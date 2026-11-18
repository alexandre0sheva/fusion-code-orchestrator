import unittest

from bst import BST
import random


class VisibleTests(unittest.TestCase):
    def test_insert_and_inorder(self):
        tree = BST()
        for key in (5, 3, 8):
            tree.insert(key)
        self.assertEqual(tree.inorder(), [3, 5, 8])

    def test_delete_a_leaf(self):
        tree = BST()
        for key in (5, 3, 8):
            tree.insert(key)
        self.assertTrue(tree.delete(3))
        self.assertEqual(tree.inorder(), [5, 8])

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
