import unittest

from wrapping import wrap


class WrapTests(unittest.TestCase):
    def test_greedy_fill(self):
        self.assertEqual(wrap("the quick brown fox jumps", 10), ["the quick", "brown fox", "jumps"])

    def test_exact_fit(self):
        self.assertEqual(wrap("abc def", 7), ["abc def"])
        self.assertEqual(wrap("abc def", 6), ["abc", "def"])

    def test_whitespace_collapses(self):
        self.assertEqual(wrap("a   b\n c\t d", 20), ["a b c d"])

    def test_paragraphs_get_one_empty_line(self):
        self.assertEqual(wrap("one two\n\n\n  \nthree", 20), ["one two", "", "three"])

    def test_leading_and_trailing_blank_lines_are_ignored(self):
        self.assertEqual(wrap("\n\nhello\n\n", 20), ["hello"])

    def test_empty_text(self):
        self.assertEqual(wrap("", 5), [])
        self.assertEqual(wrap("  \n \n", 5), [])

    def test_long_word_is_split(self):
        self.assertEqual(wrap("abcdefghij", 4), ["abcd", "efgh", "ij"])

    def test_long_word_after_text_starts_a_new_line(self):
        self.assertEqual(wrap("hi abcdefgh", 5), ["hi", "abcde", "fgh"])

    def test_last_chunk_can_be_joined_by_the_next_word(self):
        self.assertEqual(wrap("abcdefg hi", 5), ["abcde", "fg hi"])

    def test_width_one(self):
        self.assertEqual(wrap("a bc", 1), ["a", "b", "c"])

    def test_invalid_width(self):
        for width in (0, -3):
            with self.assertRaises(ValueError):
                wrap("x", width)

    def test_no_line_exceeds_the_width(self):
        text = "Pack my box with five dozen liquor jugs. Supercalifragilisticexpialidocious!"
        for line in wrap(text, 12):
            self.assertLessEqual(len(line), 12)
