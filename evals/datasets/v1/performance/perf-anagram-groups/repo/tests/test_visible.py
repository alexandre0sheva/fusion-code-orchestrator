import unittest

from anagrams import group_anagrams


class VisibleTests(unittest.TestCase):
    def test_groups(self):
        self.assertEqual(group_anagrams(["eat", "tea", "tan"]), [["eat", "tea"], ["tan"]])

    def test_empty(self):
        self.assertEqual(group_anagrams([]), [])
