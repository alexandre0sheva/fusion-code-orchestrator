import unittest

from unique import unique


class HiddenTests(unittest.TestCase):
    def test_keeps_first_occurrence_order(self):
        self.assertEqual(unique([3, 1, 2, 3, 1]), [3, 1, 2])

    def test_strings(self):
        self.assertEqual(unique(["b", "a", "b", "c", "a"]), ["b", "a", "c"])

    def test_all_equal(self):
        self.assertEqual(unique([7] * 5), [7])

    def test_does_not_modify_the_input(self):
        data = [2, 2, 1]
        unique(data)
        self.assertEqual(data, [2, 2, 1])

    def test_tuples(self):
        self.assertEqual(unique([(1, 2), (1, 2), (0, 1)]), [(1, 2), (0, 1)])

    def test_accepts_any_iterable(self):
        self.assertEqual(unique(iter([5, 4, 5])), [5, 4])
