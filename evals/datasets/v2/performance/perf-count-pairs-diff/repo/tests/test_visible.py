import unittest

from pairs import count_pairs


class VisibleTests(unittest.TestCase):
    def test_counts_steps_of_two(self):
        self.assertEqual(count_pairs([1, 3, 5], 2), 2)

    def test_no_pair(self):
        self.assertEqual(count_pairs([1, 2, 3], 5), 0)
