import unittest

from loglines import render_newest_first


class VisibleTests(unittest.TestCase):
    def test_two_lines(self):
        self.assertEqual(render_newest_first(["a", "b"]), "2\tb\n1\ta")

    def test_one_line(self):
        self.assertEqual(render_newest_first(["only"]), "1\tonly")
