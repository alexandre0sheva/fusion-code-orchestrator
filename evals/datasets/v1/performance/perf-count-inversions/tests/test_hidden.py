import unittest

from inversions import count_inversions


class HiddenTests(unittest.TestCase):
    def test_equal_values_are_not_inversions(self):
        self.assertEqual(count_inversions([2, 2, 2]), 0)

    def test_mixed_with_duplicates(self):
        self.assertEqual(count_inversions([3, 1, 2, 1]), 4)

    def test_empty_and_single(self):
        self.assertEqual(count_inversions([]), 0)
        self.assertEqual(count_inversions([5]), 0)

    def test_does_not_modify_the_input(self):
        data = [3, 2, 1]
        count_inversions(data)
        self.assertEqual(data, [3, 2, 1])

    def test_negative_and_float_values(self):
        self.assertEqual(count_inversions([0.5, -1, -1, 2.5]), 2)

    def test_matches_brute_force(self):
        data = [(i * 31) % 17 for i in range(60)]
        brute = sum(
            1 for i in range(len(data)) for j in range(i + 1, len(data)) if data[i] > data[j]
        )
        self.assertEqual(count_inversions(data), brute)
