class Node:
    def __init__(self, key):
        self.key = key
        self.left = None
        self.right = None


class BST:
    def __init__(self):
        self.root = None
        self.size = 0

    def __len__(self):
        return self.size

    def insert(self, key):
        self.size += 1
        if self.root is None:
            self.root = Node(key)
            return True
        node = self.root
        while True:
            if key == node.key:
                return False
            side = "left" if key < node.key else "right"
            child = getattr(node, side)
            if child is None:
                setattr(node, side, Node(key))
                return True
            node = child

    def contains(self, key):
        node = self.root
        while node is not None:
            if key == node.key:
                return True
            node = node.left if key < node.key else node.right
        return False

    def inorder(self):
        out = []

        def walk(node):
            if node is not None:
                walk(node.left)
                out.append(node.key)
                walk(node.right)

        walk(self.root)
        return out

    def delete(self, key):
        self.root, removed = self._delete(self.root, key)
        if removed:
            self.size -= 1
        return removed

    def _delete(self, node, key):
        if node is None:
            return None, False
        if key < node.key:
            node.left, removed = self._delete(node.left, key)
            return node, removed
        if key > node.key:
            node.right, removed = self._delete(node.right, key)
            return node, removed
        if node.left is None:
            return node.right, True
        if node.right is None:
            return node.left, True
        predecessor = node.left
        while predecessor.right is not None:
            predecessor = predecessor.right
        node.key = predecessor.key
        return node, True
