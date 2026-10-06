import unittest

from intervals import merge_intervals


class MergeIntervalsTests(unittest.TestCase):
    def test_example(self):
        self.assertEqual(merge_intervals([(1, 3), (2, 6), (8, 10), (6, 7)]), [(1, 7), (8, 10)])

    def test_touching_intervals_merge(self):
        self.assertEqual(merge_intervals([(1, 2), (2, 3)]), [(1, 3)])

    def test_separate_intervals_stay_separate(self):
        self.assertEqual(merge_intervals([(3, 4), (1, 2)]), [(1, 2), (3, 4)])

    def test_nested_interval_is_absorbed(self):
        self.assertEqual(merge_intervals([(1, 10), (2, 3), (4, 5)]), [(1, 10)])

    def test_unsorted_input(self):
        self.assertEqual(merge_intervals([(5, 6), (1, 2), (2, 5)]), [(1, 6)])

    def test_input_is_not_modified(self):
        data = [(5, 6), (1, 2)]
        merge_intervals(data)
        self.assertEqual(data, [(5, 6), (1, 2)])

    def test_single_and_empty(self):
        self.assertEqual(merge_intervals([(4, 4)]), [(4, 4)])
        self.assertEqual(merge_intervals([]), [])

    def test_invalid_interval(self):
        with self.assertRaises(ValueError):
            merge_intervals([(5, 1)])

    def test_result_holds_tuples(self):
        self.assertIsInstance(merge_intervals([(1, 2)])[0], tuple)
