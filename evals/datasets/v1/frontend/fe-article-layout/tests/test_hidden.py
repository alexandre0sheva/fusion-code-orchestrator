import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_semantic_article_structure(self):
        self.assertTrue(self.page.find("article"))
        self.assertTrue(self.page.find("header"))
        self.assertEqual(len([h for h in self.page.headings() if h[0] == 1]), 1)

    def test_publication_date_is_machine_readable(self):
        stamp = self.page.first("time")
        self.assertIsNotNone(stamp)
        self.assertRegex(stamp.get("datetime"), r"^\d{4}-\d{2}-\d{2}")

    def test_figure_has_alt_and_caption(self):
        figure = self.page.first("figure")
        self.assertIsNotNone(figure)
        parts = {e.tag for e in figure.walk()}
        self.assertIn("figcaption", parts)
        self.assertEqual(self.page.images_without_alt(), [])

    def test_three_sections_and_a_quote(self):
        self.assertGreaterEqual(len([h for h in self.page.headings() if h[0] == 2]), 3)
        self.assertTrue(self.page.find("blockquote"))

    def test_readable_line_length(self):
        widths = [v for _, v in self.page.declared("max-width")]
        self.assertTrue(any(v.endswith("ch") and 45 <= float(v[:-2]) <= 80 for v in widths), widths)

    def test_generous_line_height(self):
        heights = [v for _, v in self.page.declared("line-height")]
        numbers = [float(v) for v in heights if v.replace(".", "", 1).isdigit()]
        self.assertTrue(any(n >= 1.5 for n in numbers), heights)

    def test_fluid_layout(self):
        self.assertTrue(self.page.is_fluid())
