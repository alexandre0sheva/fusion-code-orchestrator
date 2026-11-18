import unittest

from words import top_words


class TopWordsTests(unittest.TestCase):
    def test_case_is_ignored(self):
        self.assertEqual(top_words("The the THE cat", 2), [("the", 3), ("cat", 1)])

    def test_punctuation_is_not_part_of_a_word(self):
        self.assertEqual(top_words("fox, fox. (fox) fox!", 1), [("fox", 4)])

    def test_apostrophes_inside_words(self):
        self.assertEqual(top_words("don't don't can't", 2), [("don't", 2), ("can't", 1)])

    def test_quotes_around_words_are_not_apostrophes(self):
        self.assertEqual(top_words("'hello' hello", 1), [("hello", 2)])

    def test_digits_and_underscores_separate_words(self):
        self.assertEqual(top_words("abc123def 42 abc_def", 5), [("abc", 2), ("def", 2)])

    def test_ties_are_alphabetical(self):
        self.assertEqual(top_words("pear apple fig pear apple fig kiwi", 4),
                         [("apple", 2), ("fig", 2), ("pear", 2), ("kiwi", 1)])

    def test_n_limits_and_exceeds(self):
        self.assertEqual(top_words("b a", 1), [("a", 1)])
        self.assertEqual(top_words("b a", 10), [("a", 1), ("b", 1)])

    def test_empty_and_zero(self):
        self.assertEqual(top_words("", 3), [])
        self.assertEqual(top_words("a b", 0), [])
        self.assertEqual(top_words("123 !!!", 3), [])

    def test_negative_n(self):
        with self.assertRaises(ValueError):
            top_words("a", -1)

    def test_result_is_a_list_of_tuples(self):
        result = top_words("x x y", 2)
        self.assertIsInstance(result, list)
        self.assertTrue(all(isinstance(item, tuple) for item in result))
