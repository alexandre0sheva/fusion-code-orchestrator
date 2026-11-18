import unittest

from loglines import render_newest_first


class HiddenTests(unittest.TestCase):
    def test_no_lines(self):
        self.assertEqual(render_newest_first([]), "")

    def test_positions_are_not_padded(self):
        text = render_newest_first([f"l{i}" for i in range(1, 12)])
        self.assertTrue(text.startswith("11\tl11\n10\tl10\n9\tl9"))
        self.assertTrue(text.endswith("2\tl2\n1\tl1"))

    def test_no_trailing_newline(self):
        self.assertFalse(render_newest_first(["a", "b"]).endswith("\n"))

    def test_empty_line_text_is_kept(self):
        self.assertEqual(render_newest_first(["", "x"]), "2\tx\n1\t")

    def test_text_with_tabs_is_unchanged(self):
        self.assertEqual(render_newest_first(["a\tb"]), "1\ta\tb")

    def test_accepts_any_iterable(self):
        self.assertEqual(render_newest_first(iter(["a", "b"])), "2\tb\n1\ta")
