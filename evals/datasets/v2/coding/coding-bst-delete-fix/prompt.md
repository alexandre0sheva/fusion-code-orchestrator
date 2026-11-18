`bst.py` is a binary search tree of unique keys. Two bugs are reported: after deleting a node that has two children, the key that replaced it still shows up a second time in the tree, and `len(tree)` goes up when a key that is already present is inserted again.

Fix it so that:

- `insert(key)` adds the key and returns `True`, or returns `False` and changes nothing if the key is already present.
- `delete(key)` removes the key and returns `True`, or returns `False` if it is absent. The tree stays a valid search tree and keeps every other key, whichever kind of node was removed (leaf, one child, two children, the root).
- `contains(key)`, `inorder()` (a sorted list of the keys) and `len(tree)` agree with each other at all times.
