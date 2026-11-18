import unittest

from tests.sitecheck import Page


class HiddenTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()

    def test_the_card_is_a_named_region(self):
        section = self.page.first("section")
        self.assertIsNotNone(section)
        heading = self.page.by_id(section.get("aria-labelledby"))
        self.assertIsNotNone(heading)
        self.assertTrue(heading.tag.startswith("h"))

    def test_key_figures_are_term_and_value_pairs(self):
        terms, values = self.page.find("dt"), self.page.find("dd")
        self.assertGreaterEqual(len(terms), 3)
        self.assertEqual(len(terms), len(values))
        self.assertTrue(any(any(c.isdigit() for c in v.text) for v in values))

    def test_sparkline_is_an_accessible_svg(self):
        svg = self.page.first("svg")
        self.assertIsNotNone(svg)
        self.assertEqual(svg.get("role"), "img")
        self.assertTrue(svg.get("aria-label") or self.page.first("title"))
        self.assertTrue(svg.get("viewbox") or svg.get("viewBox"))

    def test_range_selector_is_labelled_with_three_choices(self):
        select = self.page.first("select")
        self.assertIsNotNone(select)
        self.assertTrue(self.page.label_of(select))
        self.assertGreaterEqual(len(self.page.find("option")), 3)

    def test_figures_reflow(self):
        self.assertTrue(self.page.is_fluid())
        self.assertTrue(any("grid" in d for _, d in self.page.declared("display")))

    def test_one_h1_then_h2(self):
        self.assertEqual([h[0] for h in self.page.headings()][:2], [1, 2])
