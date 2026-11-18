import unittest

from listutil import unique


class UniqueTests(unittest.TestCase):
    def test_keeps_first_occurrence_order(self):
        self.assertEqual(unique([3, 1, 3, 2, 1, 4]), [3, 1, 2, 4])

    def test_strings(self):
        self.assertEqual(unique(["b", "a", "b", "c", "a"]), ["b", "a", "c"])

    def test_unhashable_items(self):
        self.assertEqual(unique([[1], [2], [1], {"a": 1}, {"a": 1}]), [[1], [2], {"a": 1}])

    def test_mixed_hashable_and_unhashable(self):
        self.assertEqual(unique([1, [1], 1, [1], "1"]), [1, [1], "1"])

    def test_key_function(self):
        self.assertEqual(unique(["Apple", "avocado", "apple", "Banana", "banana"], key=str.lower),
                         ["Apple", "avocado", "Banana"])

    def test_key_with_unhashable_keys(self):
        items = [{"id": [1], "v": "a"}, {"id": [1], "v": "b"}, {"id": [2], "v": "c"}]
        self.assertEqual([i["v"] for i in unique(items, key=lambda d: d["id"])], ["a", "c"])

    def test_input_is_not_modified(self):
        data = [2, 1, 2]
        result = unique(data)
        self.assertEqual(data, [2, 1, 2])
        self.assertIsNot(result, data)

    def test_empty_and_single(self):
        self.assertEqual(unique([]), [])
        self.assertEqual(unique([7]), [7])
