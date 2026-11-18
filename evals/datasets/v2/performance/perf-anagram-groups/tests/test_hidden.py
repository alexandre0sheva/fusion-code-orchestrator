import unittest

from anagrams import group_anagrams


class HiddenTests(unittest.TestCase):
    def test_members_keep_input_order(self):
        self.assertEqual(group_anagrams(["tea", "eat", "ate"]), [["tea", "eat", "ate"]])

    def test_groups_in_order_of_first_word(self):
        self.assertEqual(
            group_anagrams(["tan", "eat", "nat", "tea"]), [["tan", "nat"], ["eat", "tea"]]
        )

    def test_case_matters(self):
        self.assertEqual(group_anagrams(["Ab", "ba"]), [["Ab"], ["ba"]])

    def test_repeated_words_stay_in_the_group(self):
        self.assertEqual(group_anagrams(["a", "a"]), [["a", "a"]])

    def test_empty_string_is_its_own_group(self):
        self.assertEqual(group_anagrams(["", "a", ""]), [["", ""], ["a"]])

    def test_singletons(self):
        self.assertEqual(group_anagrams(["ab", "cd"]), [["ab"], ["cd"]])
