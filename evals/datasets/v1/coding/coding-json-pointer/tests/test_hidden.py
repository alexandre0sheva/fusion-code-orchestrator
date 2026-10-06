import unittest

from jsonpointer import PointerError, pointer_get, pointer_set

DOC = {
    "foo": ["bar", "baz"],
    "": 0,
    "a/b": 1,
    "c%d": 2,
    "m~n": 8,
    "~1": "tilde-one",
    "nested": {"list": [{"x": 1}, {"x": 2}]},
}


class PointerGetTests(unittest.TestCase):
    def test_rfc_examples(self):
        self.assertIs(pointer_get(DOC, ""), DOC)
        self.assertEqual(pointer_get(DOC, "/foo"), ["bar", "baz"])
        self.assertEqual(pointer_get(DOC, "/foo/0"), "bar")
        self.assertEqual(pointer_get(DOC, "/"), 0)
        self.assertEqual(pointer_get(DOC, "/a~1b"), 1)
        self.assertEqual(pointer_get(DOC, "/c%d"), 2)
        self.assertEqual(pointer_get(DOC, "/m~0n"), 8)

    def test_escape_order(self):
        self.assertEqual(pointer_get(DOC, "/~01"), "tilde-one")

    def test_deep(self):
        self.assertEqual(pointer_get(DOC, "/nested/list/1/x"), 2)

    def test_unresolvable_pointers(self):
        for pointer in ("/missing", "/foo/2", "/foo/-", "/foo/01", "/foo/-1", "/foo/x",
                        "/foo/0/deeper", "/nested/list/0/y", "/foo/1.0"):
            with self.assertRaises(PointerError, msg=pointer):
                pointer_get(DOC, pointer)

    def test_malformed_pointer(self):
        with self.assertRaises(ValueError):
            pointer_get(DOC, "foo")


class PointerSetTests(unittest.TestCase):
    def test_set_and_replace_in_dict(self):
        doc = {"a": {"b": 1}}
        self.assertIs(pointer_set(doc, "/a/c", 2), doc)
        pointer_set(doc, "/a/b", 3)
        self.assertEqual(doc, {"a": {"b": 3, "c": 2}})

    def test_list_replace_and_append(self):
        doc = {"xs": [1, 2]}
        pointer_set(doc, "/xs/0", 9)
        pointer_set(doc, "/xs/2", 3)
        pointer_set(doc, "/xs/-", 4)
        self.assertEqual(doc, {"xs": [9, 2, 3, 4]})

    def test_escaped_key(self):
        doc = {"a/b": {}}
        pointer_set(doc, "/a~1b/m~0n", 1)
        self.assertEqual(doc, {"a/b": {"m~n": 1}})

    def test_missing_parent(self):
        with self.assertRaises(PointerError):
            pointer_set({}, "/a/b", 1)

    def test_bad_list_indexes(self):
        for pointer in ("/xs/5", "/xs/01", "/xs/x", "/xs/-1"):
            with self.assertRaises(PointerError, msg=pointer):
                pointer_set({"xs": [1]}, pointer, 0)

    def test_whole_document_cannot_be_replaced(self):
        with self.assertRaises(ValueError):
            pointer_set({}, "", 1)
