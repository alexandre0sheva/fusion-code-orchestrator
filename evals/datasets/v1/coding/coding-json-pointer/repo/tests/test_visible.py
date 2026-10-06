import unittest

from jsonpointer import pointer_get


class VisibleTests(unittest.TestCase):
    def test_nested(self):
        doc = {"a": {"b": [10, 20]}}
        self.assertEqual(pointer_get(doc, "/a/b/1"), 20)

    def test_whole_document(self):
        doc = {"a": 1}
        self.assertIs(pointer_get(doc, ""), doc)

    def test_escape_order(self):
        self.assertEqual(pointer_get({"~1": "tilde-one", "/": "slash"}, "/~01"), "tilde-one")
        self.assertEqual(pointer_get({"a/b": 1, "m~n": 2}, "/a~1b"), 1)
