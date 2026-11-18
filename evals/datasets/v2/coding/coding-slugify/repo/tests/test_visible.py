import unittest

from textutil import slugify


class VisibleTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_digits_are_kept(self):
        self.assertEqual(slugify("Python 3 rocks"), "python-3-rocks")

    def test_accents_are_folded(self):
        self.assertEqual(slugify("Crème Brûlée"), "creme-brulee")
