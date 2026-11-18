import unittest

from textutil import slugify


class SlugifyTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_accents_are_folded(self):
        self.assertEqual(slugify("Crème Brûlée"), "creme-brulee")

    def test_runs_of_separators_collapse(self):
        self.assertEqual(slugify("a --- b___c"), "a-b-c")

    def test_edges_are_trimmed(self):
        self.assertEqual(slugify("  --Hi--  "), "hi")

    def test_nothing_left(self):
        self.assertEqual(slugify("!!!"), "")
        self.assertEqual(slugify(""), "")

    def test_digits(self):
        self.assertEqual(slugify("Python 3.12 rocks"), "python-3-12-rocks")

    def test_max_length_cuts(self):
        self.assertEqual(slugify("hello-world", max_length=7), "hello-w")

    def test_max_length_never_leaves_a_trailing_hyphen(self):
        self.assertEqual(slugify("hello world", max_length=6), "hello")

    def test_max_length_longer_than_slug(self):
        self.assertEqual(slugify("abc", max_length=10), "abc")
